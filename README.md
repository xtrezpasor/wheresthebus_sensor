# Where’s the Bus Home Assistant sensor

A Python command-line sensor backend for Home Assistant. It fetches rider and stop data, keeps the vendor ETA for comparison, estimates arrival from repeated fresh GPS readings, and writes local CSV history and JSONL diagnostics under `/config`.

## Private local configuration

Keep `/config/wtb_credentials.json` on Home Assistant only. It contains the WheresTheBus login and may optionally contain your local rider-name mapping. Do not commit it to GitHub. Example structure (replace the placeholders locally):

```json
{
  "email": "YOUR_WTB_EMAIL",
  "password": "YOUR_WTB_PASSWORD",
  "child_names": {
    "YOUR_RIDER_ID": "Rider name"
  }
}
```

The script applies the AM-to-PM stop fallback only when the captured AM and PM stop addresses match. No rider IDs, names, credentials, stop coordinates, session IDs, or runtime history are stored in this repository.

## Install and test

Place `wheresthebus_sensor.py` at `/config/wheresthebus_sensor.py`. Keep the existing command-line sensor YAML pointed at that path. Test from the Home Assistant host shell:

```sh
docker exec homeassistant python /config/wheresthebus_sensor.py --now
```

The command should print one JSON object containing `state` and `buses`. Daily diagnostics are written under `/config/wtb_history/diagnostics/` and retained for 30 days.
