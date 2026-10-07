#!/usr/bin/env python3
# Last updated: 2026-10-07 02:59 PM EDT (America/New_York)
"""Read WheresTheBus data for a Home Assistant command_line sensor.

Adds AM/PM stop information and a conservative GPS-movement ETA fallback.
The vendor ETA is retained as app_eta_minutes for comparison, but is not
promoted to eta_minutes because the source can be missing or wrong.

Credentials: /config/wtb_credentials.json
Session cache: /config/wtb_session.json
State/history: /config/wtb_*.json and /config/wtb_history/
"""

import csv
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = "https://mdt.wheresthebus.com"
CONFIG = Path("/config/wtb_credentials.json")
CACHE = Path("/config/wtb_session.json")
HISTORY_DIR = Path("/config/wtb_history")
HISTORY_STATE = Path("/config/wtb_history_state.json")
TRACKING_STATE = Path("/config/wtb_tracking_state.json")
ETA_SPEED_CACHE = Path("/config/wtb_eta_speed_cache.json")
DIAGNOSTICS_DIR = HISTORY_DIR / "diagnostics"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")

# Optional rider names are read from the local credentials JSON. Keep real
# names and rider IDs out of this public repository.
CHILD_NAMES = {}

HISTORY_FIELDS = (
    "child_id", "child_name", "bus", "route", "distance",
    "latitude", "longitude", "status",
)
HISTORY_COLUMNS = (
    "observed_at_utc", "child_id", "child_name", "bus", "route",
    "distance_miles", "eta", "status", "latitude", "longitude",
)

# Geometry and ETA guardrails. Distance-to-stop uses straight-line GPS
# distance; ETA is only published after several fresh samples show closing.
NEAR_STOP_MILES = 0.07
LEFT_STOP_MILES = 0.12
MAX_LOCATION_AGE_MINUTES = 3
MAX_SAMPLE_GAP_MINUTES = 8
MAX_ETA_MINUTES = 180
DIAGNOSTIC_RETENTION_DAYS = 30
HISTORICAL_ETA_LOOKBACK_DAYS = 30
HISTORICAL_ETA_CACHE_HOURS = 6
HISTORICAL_ETA_MIN_SAMPLES = 10
HISTORICAL_ETA_MIN_DAYS = 1
RUN_ID = "startup"


def log_event(event, **fields):
    """Append one private JSONL diagnostic record; never log secrets."""
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(LOCAL_TIMEZONE)
    try:
        DIAGNOSTICS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(DIAGNOSTICS_DIR, 0o700)
        record = {
            "at_local": local_now.isoformat(timespec="seconds"),
            "at_utc": now.isoformat(timespec="seconds"),
            "run_id": RUN_ID,
            "event": event,
            **fields,
        }
        line = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode()
        path = DIAGNOSTICS_DIR / f"{local_now.date().isoformat()}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except Exception:
        # Diagnostics must never prevent the sensor from returning its data.
        pass


def prune_diagnostic_logs():
    """Remove past weekend logs and logs older than the retention window."""
    today = datetime.now(LOCAL_TIMEZONE).date()
    cutoff = today - timedelta(days=DIAGNOSTIC_RETENTION_DAYS)
    removed = 0
    # These directories contain dated daily files only. Keep today's file,
    # including on weekends, so an active day's diagnostics remain available.
    for directory, pattern in ((DIAGNOSTICS_DIR, "*.jsonl"), (HISTORY_DIR, "*.csv")):
        try:
            paths = directory.glob(pattern)
            for path in paths:
                try:
                    log_day = datetime.strptime(path.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                expired = log_day < cutoff
                past_weekend = log_day < today and log_day.weekday() >= 5
                if expired or past_weekend:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
        except OSError:
            continue
    if removed:
        log_event("history_logs_pruned", removed_file_count=removed,
                  retention_days=DIAGNOSTIC_RETENTION_DAYS,
                  weekend_logs_removed=True)


def request(url, body):
    if urlsplit(url).hostname != "mdt.wheresthebus.com":
        raise ValueError("Unexpected API host")
    data = json.dumps(body, separators=(",", ":")).encode()
    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://parentapp.wheresthebus.com",
            "Referer": "https://parentapp.wheresthebus.com/",
        },
        method="POST",
    )
    endpoint = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    started = datetime.now(timezone.utc)
    try:
        with urlopen(req, timeout=12) as response:
            result = json.load(response)
            elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
            log_event("api_response", endpoint=endpoint, outcome="success",
                      http_status=response.status, elapsed_seconds=elapsed)
            return result, None
    except HTTPError as exc:
        if exc.code in (307, 308):
            log_event("api_response", endpoint=endpoint, outcome="redirect",
                      http_status=exc.code)
            return None, exc.headers.get("Location")
        log_event("api_response", endpoint=endpoint, outcome="http_error",
                  http_status=exc.code)
        raise
    except Exception as exc:
        log_event("api_response", endpoint=endpoint, outcome="error",
                  error_type=type(exc).__name__)
        raise


def api(base, endpoint, body, session=None):
    url = urljoin(base.rstrip("/") + "/", endpoint)
    if session:
        url += "?sessionId=" + quote(session, safe="")
    result, redirect = request(url, body)
    if redirect:
        result, second_redirect = request(urljoin(url, redirect), body)
        if second_redirect:
            raise RuntimeError("Unexpected second API redirect")
    if not isinstance(result, dict) or result.get("resCode") != 0:
        log_event("api_rejected", endpoint=endpoint,
                  response_code=result.get("resCode") if isinstance(result, dict) else None)
        raise RuntimeError("WheresTheBus API rejected the request")
    return result["payload"]


def write_private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wtb-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def login(credentials, cache):
    device = cache.get("device_id") or str(uuid4())
    data = api(
        ROOT + "/wtbparentapp/api/v2/", "login",
        {
            "emailId": credentials["email"],
            "password": credentials["password"],
            "imeiNo": device,
            "deviceType": "FlutterWeb",
            "sso": 0,
            "deviceOS": "Web_chrome_Flutter",
        },
    )
    base = data["basePath"].rstrip("/") + "/wtbparentapp/api/v2/"
    if urlsplit(base).hostname != "mdt.wheresthebus.com":
        raise ValueError("Unexpected shard host")
    cache = {"device_id": device, "session_id": data["sessionId"], "base": base}
    write_private_json(CACHE, cache)
    return cache


def valid_coordinate(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return 39.0 <= lat <= 42.5 and -75.5 <= lon <= -71.0


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def parse_app_eta(value):
    """Return integer minutes only for a plain numeric ETA; otherwise None."""
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*", str(value))
    if not match:
        return None
    return int(match.group(1))


def status_age_minutes(status):
    """Parse API status such as 'current', '1 min. ago', or 'inactive'."""
    if not status:
        return None
    s = str(status).strip().lower()
    if s == "current":
        return 0.0
    m = re.search(r"(\d+)\s*min", s)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*sec", s)
    if m:
        return int(m.group(1)) / 60.0
    return None


def haversine_miles(lat1, lon1, lat2, lon2):
    radius_miles = 3958.7613
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_miles * math.asin(min(1.0, math.sqrt(a)))


def valid_stop(lat, lon):
    return valid_coordinate(lat, lon) and not (float(lat) == 0 and float(lon) == 0)


def clean_stop(rider, period):
    """Return stop metadata; allow AM-to-PM fallback only for matching addresses."""
    prefix = "am" if period == "am" else "pm"
    lat = rider.get(prefix + "StopLat")
    lon = rider.get(prefix + "StopLon")
    stop_id = rider.get(prefix + "StopId")
    address = rider.get(prefix + "StopAddress")
    stop_time = rider.get(prefix + "StopTime")
    source = period

    if not valid_stop(lat, lon) and period == "am":
        # Use the PM location only when the captured AM and PM addresses match.
        am_address = str(rider.get("amStopAddress") or "").strip().casefold()
        pm_address = str(rider.get("pmStopAddress") or "").strip().casefold()
        if am_address and am_address == pm_address and valid_stop(
            rider.get("pmStopLat"), rider.get("pmStopLon")
        ):
            lat, lon = rider.get("pmStopLat"), rider.get("pmStopLon")
            stop_id = rider.get("pmStopId")
            source = "pm_fallback_same_address"

    return {
        "stop_period": period,
        "stop_id": stop_id,
        "stop_time": stop_time,
        "stop_latitude": number(lat) if valid_stop(lat, lon) else None,
        "stop_longitude": number(lon) if valid_stop(lat, lon) else None,
        "stop_source": source if valid_stop(lat, lon) else "unavailable",
    }


def find_rider_stop(stop_rows, route):
    if route is None:
        return None
    route = str(route).strip().casefold()
    for row in stop_rows:
        if route in {
            str(row.get("amBusNo") or "").strip().casefold(),
            str(row.get("pmBusNo") or "").strip().casefold(),
        }:
            return row
    return None


def choose_period(now):
    return "am" if now.astimezone(LOCAL_TIMEZONE).hour < 12 else "pm"


def estimate_eta_from_history(samples, current_distance, now):
    """Return ETA, reason, closing speed, and interval count."""
    if current_distance is None:
        return None, "no_fresh_bus_location_or_stop", None, 0
    if current_distance <= NEAR_STOP_MILES:
        return 0, "inside_arrival_radius", None, 0
    closing_rates = []
    # Require the latest distinct sample to show progress toward the stop;
    # older approach samples must not produce an ETA after the bus turns away.
    if samples:
        try:
            latest_distance = float(samples[-1]["distance"])
        except (KeyError, TypeError, ValueError):
            return None, "previous_sample_invalid", None, 0
        if latest_distance - current_distance < 0.015:
            return None, "latest_sample_not_closing", None, 0
    for old in samples[-5:]:
        try:
            old_at = datetime.fromisoformat(old["at"])
            old_dist = float(old["distance"])
        except (KeyError, TypeError, ValueError):
            continue
        minutes = (now - old_at).total_seconds() / 60.0
        if minutes < 0.5 or minutes > MAX_SAMPLE_GAP_MINUTES:
            continue
        closing = old_dist - current_distance
        # Ignore GPS jitter and buses that are not measurably approaching.
        if closing >= 0.015:
            mph = closing / (minutes / 60.0)
            if 2.0 <= mph <= 55.0:
                closing_rates.append(mph)
    # At least two independent intervals (three samples) before estimating.
    if len(closing_rates) < 2:
        return None, "need_more_closing_samples", None, len(closing_rates)
    mph = median(closing_rates)
    eta = int(round((current_distance / mph) * 60.0))
    if 0 <= eta <= MAX_ETA_MINUTES:
        return eta, "gps_movement", round(mph, 2), len(closing_rates)
    return None, "calculated_eta_out_of_range", round(mph, 2), len(closing_rates)


def distance_band(distance):
    """Group prior bus speeds by approximate distance from this stop."""
    if distance <= 0.5:
        return "0-0.5"
    if distance <= 1.5:
        return "0.5-1.5"
    if distance <= 3.0:
        return "1.5-3"
    return "3+"


def load_historical_speed_profiles(now):
    """Learn conservative per-rider speeds from prior days' private diagnostics.

    Only adjacent fresh GPS observations from the same service day, route,
    rider, and stop period are used. Today's data is excluded so this is a
    genuine prior-days baseline, not a second copy of the live estimate.
    """
    try:
        if ETA_SPEED_CACHE.exists():
            cached = json.loads(ETA_SPEED_CACHE.read_text())
            built_at = datetime.fromisoformat(cached.get("built_at", ""))
            if now - built_at < timedelta(hours=HISTORICAL_ETA_CACHE_HOURS):
                return cached.get("profiles", {})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    today = now.astimezone(LOCAL_TIMEZONE).date()
    cutoff = today - timedelta(days=HISTORICAL_ETA_LOOKBACK_DAYS)
    # Keep speed observations grouped by rider, route, period, distance band,
    # and service day. Requiring multiple days avoids trusting one unusual trip.
    speeds = {}
    previous = {}
    try:
        paths = sorted(DIAGNOSTICS_DIR.glob("*.jsonl"))
    except OSError:
        paths = []

    for path in paths:
        try:
            service_day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if service_day < cutoff or service_day >= today:
            continue
        try:
            with path.open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if row.get("event") != "child_assessment":
                            continue
                        if not row.get("location_fresh"):
                            continue
                        distance = number(row.get("stop_distance_miles"))
                        child_id = str(row.get("child_id") or "")
                        route = str(row.get("route") or "")
                        period = str(row.get("stop_period") or "")
                        observed = datetime.fromisoformat(row.get("at_utc", ""))
                        if (distance is None or not child_id or not route
                                or period not in ("am", "pm")):
                            continue
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue

                    key = (child_id, route, period, service_day.isoformat())
                    old = previous.get(key)
                    if old:
                        elapsed = (observed - old[0]).total_seconds() / 60.0
                        closing = old[1] - distance
                        if 0.5 <= elapsed <= MAX_SAMPLE_GAP_MINUTES and closing >= 0.015:
                            mph = closing * 60.0 / elapsed
                            if 2.0 <= mph <= 55.0:
                                band = distance_band((old[1] + distance) / 2.0)
                                profile_key = "|".join((child_id, route, period, band))
                                entry = speeds.setdefault(profile_key, {})
                                entry.setdefault(service_day.isoformat(), []).append(mph)
                    previous[key] = (observed, distance)
        except OSError:
            continue

    profiles = {}
    for key, by_day in speeds.items():
        observations = [speed for daily in by_day.values() for speed in daily]
        if (len(observations) >= HISTORICAL_ETA_MIN_SAMPLES
                and len(by_day) >= HISTORICAL_ETA_MIN_DAYS):
            profiles[key] = {
                "median_mph": round(median(observations), 2),
                "sample_count": len(observations),
                "day_count": len(by_day),
                "confidence": "medium" if len(by_day) >= 3 else "low",
            }

    try:
        write_private_json(ETA_SPEED_CACHE, {
            "built_at": now.isoformat(timespec="seconds"),
            "profiles": profiles,
        })
    except OSError:
        pass
    log_event("historical_eta_profiles_loaded", profile_count=len(profiles),
              lookback_days=HISTORICAL_ETA_LOOKBACK_DAYS)
    return profiles


def estimate_eta_from_prior_days(profiles, child_id, route, period, distance):
    """Use a multi-day median speed only when live GPS is fresh."""
    if distance is None or distance <= NEAR_STOP_MILES:
        return None, None
    key = "|".join((str(child_id), str(route or ""), period, distance_band(distance)))
    profile = profiles.get(key)
    if not profile:
        return None, None
    try:
        mph = float(profile["median_mph"])
        eta = int(round(distance / mph * 60.0))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None, None
    if 0 <= eta <= MAX_ETA_MINUTES:
        return eta, profile
    return None, None


def read_buses(cache, tracking_state, historical_profiles):
    session = cache["session_id"]
    base = cache["base"]
    user = api(
        base, "getUserInfo",
        {
            "imeiNo": cache["device_id"],
            "versionInstalled": "5.2.2",
            "tokenId": "",
            "deviceNotif": False,
            "sessionId": session,
        },
        session,
    )
    log_event("user_info_loaded", child_count=len(user.get("childBuses", [])))
    # Captured parent-app call: POST getAllRiders with {"sessionId": ...}.
    stops_payload = api(
        base, "getAllRiders", {"sessionId": session}, session
    )
    stop_rows = stops_payload.get("allRiders", [])
    log_event("stops_loaded", rider_count=len(stop_rows))
    now = datetime.now(timezone.utc)
    period = choose_period(now)
    buses = []
    new_track = {}

    for child in user.get("childBuses", []):
        child_id = int(child["childId"])
        route = child.get("routeNo")
        rider = api(
            base, "getRiderInfoEx",
            {
                "bid": child["busNo"],
                "chdId": child["childId"],
                "lastServerTime": 0,
                "sessionId": session,
            },
            session,
        )
        stop_row = find_rider_stop(stop_rows, route)
        stop = clean_stop(stop_row or {}, period)

        lat = number(rider.get("busLat"))
        lon = number(rider.get("busLon"))
        status = rider.get("stsMsg")
        age = status_age_minutes(status)
        gps_valid = valid_coordinate(lat, lon)
        gps_fresh = gps_valid and age is not None and age <= MAX_LOCATION_AGE_MINUTES

        stop_distance = None
        if gps_fresh and stop["stop_latitude"] is not None:
            stop_distance = round(haversine_miles(
                lat, lon, stop["stop_latitude"], stop["stop_longitude"]
            ), 3)

        track_key = f"{child_id}:{period}"
        previous = tracking_state.get(track_key, {})
        service_day = now.astimezone(LOCAL_TIMEZONE).date().isoformat()
        # Tracking and drop-off status must never carry into a new local day.
        if previous.get("service_day") != service_day:
            previous = {}
        samples = previous.get("samples", [])
        if not isinstance(samples, list):
            samples = []
        samples = samples[-5:]
        prior_sample_count = len(samples)

        backup_eta, eta_reason, closing_speed, closing_intervals = estimate_eta_from_history(
            samples, stop_distance, now
        )
        eta_source = "gps_movement" if backup_eta is not None else "unavailable"
        eta_confidence = "medium" if backup_eta is not None else "unavailable"
        historical_profile = None
        # If fresh live GPS has at least one recent closing interval but not
        # enough intervals for a live ETA, use this rider's prior-trip speed.
        # Never use old-day speeds when today's bus location is stale or not
        # currently moving toward the stop.
        if backup_eta is None and gps_fresh and closing_intervals >= 1:
            backup_eta, historical_profile = estimate_eta_from_prior_days(
                historical_profiles, child_id, route, period, stop_distance
            )
            if backup_eta is not None:
                eta_source = "historical_speed"
                eta_confidence = historical_profile.get("confidence", "low")
                eta_reason = "prior_days_median_speed"
        if gps_fresh and stop_distance is not None:
            # Keep samples only from valid, fresh bus locations. Avoid storing
            # repeated identical coordinates, which create false zero speeds.
            last_sample = samples[-1] if samples else None
            if not last_sample or (
                round(float(last_sample.get("lat", 0)), 5) != round(lat, 5)
                or round(float(last_sample.get("lon", 0)), 5) != round(lon, 5)
            ):
                samples.append({
                    "at": now.isoformat(timespec="seconds"),
                    "distance": stop_distance,
                    "lat": lat,
                    "lon": lon,
                })
            samples = samples[-6:]

        was_near = previous.get("near_stop_at")
        near_now = gps_fresh and stop_distance is not None and stop_distance <= NEAR_STOP_MILES
        if near_now and period == "pm":
            near_at = now.isoformat(timespec="seconds")
        else:
            near_at = was_near

        dropoff_at = previous.get("dropoff_likely_at")
        new_dropoff_detected = False
        if period == "pm" and gps_fresh and stop_distance is not None and was_near:
            try:
                candidate_at = datetime.fromisoformat(was_near)
                elapsed = now - candidate_at
                new_dropoff_detected = (
                    timedelta(0) <= elapsed <= timedelta(minutes=20)
                    and stop_distance >= LEFT_STOP_MILES
                )
            except (TypeError, ValueError):
                pass
        if new_dropoff_detected and not dropoff_at:
            dropoff_at = now.isoformat(timespec="seconds")
        dropped_off_likely = bool(dropoff_at)
        if dropped_off_likely:
            near_at = None
        elif near_at:
            try:
                if now - datetime.fromisoformat(near_at) > timedelta(minutes=20):
                    near_at = None
            except (TypeError, ValueError):
                near_at = None

        # Once the bus has likely completed this rider's stop, later bus GPS
        # movement must not create another arrival or ETA for the same day.
        if dropped_off_likely:
            backup_eta = None
            eta_source = "unavailable"
            eta_confidence = "unavailable"
            eta_reason = "likely_dropoff_already_detected"
        new_track[track_key] = {
            "service_day": service_day,
            "samples": samples,
            "near_stop_at": near_at,
            "dropoff_likely_at": dropoff_at,
        }
        app_eta = parse_app_eta(rider.get("etaMsg"))
        app_distance = number(rider.get("dist"))
        # Respect API unit flag when converting its displayed distance.
        if rider.get("isDistKm") in (1, True, "1") and app_distance is not None:
            app_distance *= 0.621371

        if stop["stop_latitude"] is None:
            arrival_reason = "selected_stop_unavailable"
        elif not gps_fresh:
            arrival_reason = "bus_location_stale_or_missing"
        elif near_now:
            arrival_reason = "fresh_gps_inside_arrival_radius"
        else:
            arrival_reason = "fresh_gps_outside_arrival_radius"

        arrival_likely = bool(near_now and not dropped_off_likely)
        if dropped_off_likely and not new_dropoff_detected:
            arrival_reason = "likely_dropoff_already_detected_for_service_day"

        if period != "pm":
            dropoff_reason = "not_pm_route_window"
        elif not gps_fresh:
            dropoff_reason = "bus_location_stale_or_missing"
        elif dropoff_at:
            dropoff_reason = (
                "bus_departed_stop_after_recent_proximity"
                if new_dropoff_detected
                else "likely_dropoff_already_detected_for_service_day"
            )
        elif near_now:
            dropoff_reason = "bus_near_stop_waiting_for_departure_evidence"
        elif was_near:
            dropoff_reason = "recent_stop_visit_but_not_yet_departed_far_enough"
        else:
            dropoff_reason = "no_recent_stop_proximity_sample"

        bus_record = {
            "child_id": child_id,
            "child_name": CHILD_NAMES.get(child_id, "Unknown"),
            "bus": child.get("busNo"),
            "route": route,
            # Kept for compatibility with existing Home Assistant automations.
            "eta": rider.get("etaMsg"),
            "status": status,
            "distance": round(app_distance, 2) if app_distance is not None else None,
            "latitude": lat if gps_valid else 0.0,
            "longitude": lon if gps_valid else 0.0,
            "app_eta_minutes": app_eta,
            # A backup ETA is only published from repeated, fresh GPS movement.
            "eta_minutes": backup_eta,
            "eta_source": eta_source,
            "eta_confidence": eta_confidence,
            "eta_reason": eta_reason,
            "location_age_minutes": round(age, 1) if age is not None else None,
            "location_fresh": bool(gps_fresh),
            "stop_period": stop["stop_period"],
            "stop_id": stop["stop_id"],
            "stop_time": stop["stop_time"],
            "stop_latitude": stop["stop_latitude"],
            "stop_longitude": stop["stop_longitude"],
            "stop_source": stop["stop_source"],
            "stop_distance_miles": stop_distance,
            "arrival_likely": arrival_likely,
            "arrival_reason": arrival_reason,
            "dropped_off_likely": bool(dropped_off_likely),
            "dropoff_reason": dropoff_reason,
        }
        buses.append(bus_record)
        log_event(
            "child_assessment",
            child_id=child_id,
            child_name=bus_record["child_name"],
            bus=bus_record["bus"],
            route=route,
            status=status,
            app_eta=rider.get("etaMsg"),
            app_distance_miles=bus_record["distance"],
            bus_latitude=lat if gps_valid else None,
            bus_longitude=lon if gps_valid else None,
            location_age_minutes=bus_record["location_age_minutes"],
            location_fresh=bool(gps_fresh),
            stop_period=period,
            stop_source=stop["stop_source"],
            stop_id=stop["stop_id"],
            stop_latitude=stop["stop_latitude"],
            stop_longitude=stop["stop_longitude"],
            stop_distance_miles=stop_distance,
            prior_sample_count=prior_sample_count,
            sample_count_after=len(samples),
            closing_speed_mph=closing_speed,
            closing_intervals=closing_intervals,
            eta_minutes=backup_eta,
            eta_source=eta_source,
            eta_confidence=eta_confidence,
            historical_speed_profile=historical_profile,
            eta_reason=eta_reason,
            arrival_likely=arrival_likely,
            arrival_reason=arrival_reason,
            dropped_off_likely=bool(dropped_off_likely),
            dropoff_reason=dropoff_reason,
        )

    write_private_json(TRACKING_STATE, new_track)
    return buses


def record_history(buses, force=False):
    previous = json.loads(HISTORY_STATE.read_text()) if HISTORY_STATE.exists() else {}
    now = datetime.now(timezone.utc)
    observed_at = now.isoformat(timespec="seconds")
    local_day = now.astimezone(LOCAL_TIMEZONE).date().isoformat()
    rows = []
    for bus in buses:
        child_id = str(bus["child_id"])
        values = {field: bus.get(field) for field in HISTORY_FIELDS}
        last = previous.get(child_id, {})
        try:
            last_logged = datetime.fromisoformat(last["observed_at"])
        except (KeyError, ValueError, TypeError):
            last_logged = None
        unchanged = last.get("values") == values
        recent = last_logged is not None and now - last_logged < timedelta(minutes=15)
        same_day = last.get("local_day") == local_day
        if unchanged and recent and same_day and not force:
            continue
        rows.append(bus)
        previous[child_id] = {
            "values": values, "observed_at": observed_at, "local_day": local_day,
        }
    if not rows:
        return 0

    HISTORY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    history_path = HISTORY_DIR / f"{local_day}.csv"
    fd = os.open(history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", newline="") as history:
        os.chmod(history_path, 0o600)
        writer = csv.writer(history)
        if os.fstat(history.fileno()).st_size == 0:
            writer.writerow(HISTORY_COLUMNS)
        for bus in rows:
            writer.writerow([
                observed_at, bus.get("child_id"), bus.get("child_name"),
                bus.get("bus"), bus.get("route"), bus.get("distance"),
                bus.get("eta"), bus.get("status"), bus.get("latitude"),
                bus.get("longitude"),
            ])
    write_private_json(HISTORY_STATE, previous)
    return len(rows)


def main():
    global RUN_ID, CHILD_NAMES
    RUN_ID = uuid4().hex[:10]
    started = datetime.now(timezone.utc)
    prune_diagnostic_logs()
    log_event("run_started", force_history="--now" in sys.argv[1:])
    credentials = json.loads(CONFIG.read_text())
    configured_names = credentials.get("child_names", {})
    if isinstance(configured_names, dict):
        CHILD_NAMES = {
            int(child_id): str(name)
            for child_id, name in configured_names.items()
        }
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    tracking_state = json.loads(TRACKING_STATE.read_text()) if TRACKING_STATE.exists() else {}
    historical_profiles = load_historical_speed_profiles(started)
    for attempt in range(2):
        if not cache.get("session_id"):
            log_event("login_started", attempt=attempt + 1)
            cache = login(credentials, cache)
            log_event("login_succeeded", attempt=attempt + 1)
        try:
            buses = read_buses(cache, tracking_state, historical_profiles)
            history_rows = record_history(buses, force="--now" in sys.argv[1:])
            elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
            log_event("run_completed", state="online", child_count=len(buses),
                      history_rows_written=history_rows, elapsed_seconds=elapsed)
            print(json.dumps(
                {"state": "online", "buses": buses},
                separators=(",", ":"),
            ))
            return
        except (RuntimeError, HTTPError):
            log_event("api_attempt_failed", attempt=attempt + 1,
                      error_type="HTTPError_or_API_error")
            if attempt:
                raise
            log_event("session_refresh", attempt=attempt + 1)
            cache = login(credentials, cache)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_event("run_failed", error_type=type(exc).__name__)
        # Do not print credentials, session IDs, or locations on errors.
        print(json.dumps({
            "state": "error", "buses": [], "error_type": type(exc).__name__,
        }))
        sys.exit(0)
