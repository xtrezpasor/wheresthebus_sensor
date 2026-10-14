#!/usr/bin/env python3
# Last updated: 2026-10-14 01:20 PM EDT (America/New_York)
"""Build a private route-history model from Where's the Bus diagnostics.

Run once daily, or manually with:
    python /config/wheresthebus_route_learner.py

The output is local-only at /config/wtb_route_model.json. It contains historic
GPS points needed to match today's bus position to earlier trips. It does not
send data to Ollama or any external service.
"""

import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

HISTORY_DIR = Path("/config/wtb_history")
DIAGNOSTICS_DIR = HISTORY_DIR / "diagnostics"
OUTPUT = Path("/config/wtb_route_model.json")
LOCK = Path("/config/wtb_route_learner.lock")
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 30
ARRIVAL_RADIUS_MILES = 0.10
PAUSE_RADIUS_MILES = 0.035
PAUSE_MIN_SECONDS = 75
MAX_SAMPLE_GAP_MINUTES = 4
MIN_TRIP_POINTS = 6
MIN_MATCH_DISTANCE_MILES = 0.10
MIN_TRIP_DAYS_FOR_PREDICTION = 3
MIN_DAYS_FOR_CONFIDENT_STOPS = 3
MAX_POINTS_PER_TRIP = 120


def miles_between(lat1, lon1, lat2, lon2):
    radius = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def valid_coordinate(lat, lon):
    return 39.0 <= lat <= 42.5 and -75.5 <= lon <= -71.0


def load_observations(now):
    cutoff = now.astimezone(LOCAL_TIMEZONE).date() - timedelta(days=LOOKBACK_DAYS)
    grouped = defaultdict(list)
    for path in sorted(DIAGNOSTICS_DIR.glob("*.jsonl")):
        try:
            service_day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        # The in-progress service day is excluded so a trip is never learned
        # from a partial route and used to predict itself.
        if service_day < cutoff or service_day >= now.astimezone(LOCAL_TIMEZONE).date():
            continue
        try:
            with path.open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if row.get("event") != "child_assessment" or not row.get("location_fresh"):
                            continue
                        child_id = str(row.get("child_id") or "")
                        route = str(row.get("route") or "")
                        period = str(row.get("stop_period") or "")
                        at = datetime.fromisoformat(row.get("at_utc", ""))
                        lat = float(row.get("bus_latitude"))
                        lon = float(row.get("bus_longitude"))
                        distance = float(row.get("stop_distance_miles"))
                        stop_lat = float(row.get("stop_latitude"))
                        stop_lon = float(row.get("stop_longitude"))
                        if (not child_id or not route or period not in ("am", "pm")
                                or not valid_coordinate(lat, lon)
                                or not valid_coordinate(stop_lat, stop_lon)
                                or not math.isfinite(distance)):
                            continue
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    key = (child_id, route, period, service_day.isoformat())
                    grouped[key].append({
                        "at": at, "lat": lat, "lon": lon,
                        "distance": distance,
                        "stop_lat": stop_lat, "stop_lon": stop_lon,
                    })
        except OSError:
            continue
    return grouped


def make_trip(rows):
    rows.sort(key=lambda row: row["at"])
    # Remove duplicate GPS fixes while retaining timestamps for dwell analysis.
    points = []
    for row in rows:
        if points and row["at"] <= points[-1]["at"]:
            continue
        points.append(row)
    if len(points) < MIN_TRIP_POINTS:
        return None

    arrival_index = next((i for i, p in enumerate(points)
                          if p["distance"] <= ARRIVAL_RADIUS_MILES), None)
    if arrival_index is None or arrival_index < MIN_TRIP_POINTS - 1:
        return None
    arrival = points[arrival_index]["at"]
    route_points = points[:arrival_index + 1]
    # A bus commonly waits at school before starting the run. Recognize a
    # sustained GPS cluster at the beginning of the trace, retain it as the
    # learned school origin, and start route timing after the bus departs.
    school_origin = None
    origin_center = (route_points[0]["lat"], route_points[0]["lon"])
    departure_index = None
    for index, point in enumerate(route_points[1:], start=1):
        gap = (point["at"] - route_points[index - 1]["at"]).total_seconds() / 60
        if gap > MAX_SAMPLE_GAP_MINUTES:
            break
        moved = miles_between(*origin_center, point["lat"], point["lon"])
        if moved > PAUSE_RADIUS_MILES:
            dwell_seconds = (route_points[index - 1]["at"] - route_points[0]["at"]).total_seconds()
            if dwell_seconds >= PAUSE_MIN_SECONDS:
                origin_points = route_points[:index]
                school_origin = {
                    "latitude": round(median(p["lat"] for p in origin_points), 6),
                    "longitude": round(median(p["lon"] for p in origin_points), 6),
                    "dwell_minutes": round(dwell_seconds / 60, 1),
                }
                departure_index = index
            break
    if departure_index is not None:
        route_points = route_points[departure_index:]
        arrival = route_points[-1]["at"]
    if len(route_points) < MIN_TRIP_POINTS:
        return None
    if (route_points[-1]["at"] - route_points[0]["at"]).total_seconds() < 180:
        return None

    # Thin the polyline while preserving movement and time-to-stop detail.
    stride = max(1, math.ceil(len(route_points) / MAX_POINTS_PER_TRIP))
    stored = []
    last_stored = None
    for i, point in enumerate(route_points):
        mins_left = max(0.0, (arrival - point["at"]).total_seconds() / 60.0)
        candidate = {
            "lat": round(point["lat"], 6),
            "lon": round(point["lon"], 6),
            "minutes_to_stop": round(mins_left, 2),
        }
        moved = (last_stored is None or miles_between(
            last_stored["lat"], last_stored["lon"], point["lat"], point["lon"]
        ) >= 0.02)
        if i == 0 or i == len(route_points) - 1 or i % stride == 0 or moved:
            stored.append(candidate)
            last_stored = candidate
    if stored[-1]["minutes_to_stop"] != 0:
        p = route_points[-1]
        stored.append({"lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                       "minutes_to_stop": 0.0})

    # Identify pauses, but only publish them as likely stops after they recur
    # across multiple separate service days.
    pauses = []
    cluster = []

    def finish_cluster(items):
        if len(items) < 2:
            return
        duration = (items[-1]["at"] - items[0]["at"]).total_seconds()
        if duration < PAUSE_MIN_SECONDS:
            return
        lat = median(p["lat"] for p in items)
        lon = median(p["lon"] for p in items)
        center_at = items[0]["at"] + (items[-1]["at"] - items[0]["at"]) / 2
        pauses.append({
            "lat": round(lat, 6), "lon": round(lon, 6),
            "minutes_to_stop": round(max(0.0, (arrival - center_at).total_seconds() / 60), 2),
            "dwell_minutes": round(duration / 60, 2),
        })

    for point in route_points:
        if cluster:
            center_lat = median(p["lat"] for p in cluster)
            center_lon = median(p["lon"] for p in cluster)
            gap = (point["at"] - cluster[-1]["at"]).total_seconds() / 60
            if (gap > MAX_SAMPLE_GAP_MINUTES or miles_between(
                    center_lat, center_lon, point["lat"], point["lon"]
            ) > PAUSE_RADIUS_MILES):
                finish_cluster(cluster)
                cluster = []
        cluster.append(point)
    finish_cluster(cluster)

    return {
        "service_day": route_points[0]["at"].astimezone(LOCAL_TIMEZONE).date().isoformat(),
        "arrival_at": arrival.isoformat(timespec="seconds"),
        "stop_lat": round(route_points[-1]["stop_lat"], 6),
        "stop_lon": round(route_points[-1]["stop_lon"], 6),
        "school_origin": school_origin,
        "points": stored,
        "pauses": pauses,
    }


def consolidate_pauses(trips):
    candidates = []
    for trip in trips:
        for pause in trip["pauses"]:
            match = next((candidate for candidate in candidates
                          if trip["service_day"] not in candidate["days"]
                          and miles_between(candidate["lat"], candidate["lon"],
                                            pause["lat"], pause["lon"]) <= 0.08), None)
            if match is None:
                match = {"lat": pause["lat"], "lon": pause["lon"],
                         "days": set(), "minutes": [], "dwell": []}
                candidates.append(match)
            match["days"].add(trip["service_day"])
            match["minutes"].append(pause["minutes_to_stop"])
            match["dwell"].append(pause["dwell_minutes"])
            match["lat"] = round(median([match["lat"], pause["lat"]]), 6)
            match["lon"] = round(median([match["lon"], pause["lon"]]), 6)
    result = []
    for candidate in candidates:
        if len(candidate["days"]) < MIN_DAYS_FOR_CONFIDENT_STOPS:
            continue
        result.append({
            "latitude": candidate["lat"], "longitude": candidate["lon"],
            "days_seen": len(candidate["days"]),
            "median_minutes_to_child_stop": round(median(candidate["minutes"]), 1),
            "median_dwell_minutes": round(median(candidate["dwell"]), 1),
        })
    return sorted(result, key=lambda stop: stop["median_minutes_to_child_stop"], reverse=True)


def write_private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".wtb-route-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, separators=(",", ":"))
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def build_model(now=None):
    now = now or datetime.now(timezone.utc)
    grouped = load_observations(now)
    trips_by_profile = defaultdict(list)
    for (child_id, route, period, _day), rows in grouped.items():
        trip = make_trip(rows)
        if trip:
            trips_by_profile[(child_id, route, period)].append(trip)

    profiles = {}
    for (child_id, route, period), trips in trips_by_profile.items():
        trips.sort(key=lambda trip: trip["service_day"])
        # Keep only the latest completed service trips in the retention window.
        trips = trips[-30:]
        profiles["|".join((child_id, route, period))] = {
            "child_id": child_id,
            "route": route,
            "stop_period": period,
            "trip_days": len({trip["service_day"] for trip in trips}),
            "trips": trips,
            "probable_stops": consolidate_pauses(trips),
            "school_origin": {
                "latitude": round(median(
                    trip["school_origin"]["latitude"] for trip in trips
                    if trip.get("school_origin")
                ), 6),
                "longitude": round(median(
                    trip["school_origin"]["longitude"] for trip in trips
                    if trip.get("school_origin")
                ), 6),
                "days_seen": sum(1 for trip in trips if trip.get("school_origin")),
            } if sum(1 for trip in trips if trip.get("school_origin")) >= 3 else None,
        }
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "minimum_trip_days_for_prediction": MIN_TRIP_DAYS_FOR_PREDICTION,
        "profiles": profiles,
    }


if __name__ == "__main__":
    try:
        model = build_model()
        write_private_json(OUTPUT, model)
        trip_count = sum(profile["trip_days"] for profile in model["profiles"].values())
        pause_count = sum(len(profile["probable_stops"]) for profile in model["profiles"].values())
        print(json.dumps({
            "written": str(OUTPUT),
            "profiles": len(model["profiles"]),
            "completed_trip_days": trip_count,
            "recurrent_stops": pause_count,
            "generated_at": model["generated_at"],
        }))
    finally:
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass
