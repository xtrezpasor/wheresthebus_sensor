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

## Alert when the sensor has an error

The Python script emits `state: error` and an `error_type` attribute when login, API, configuration, or another run fails. Home Assistant must reload that JSON through the existing command-line sensor. Add the automation below to `automations.yaml` (or import `bus_error_alert.yaml` in your Home Assistant setup), then reload automations. It sends one phone alert when the sensor first enters the error state, avoiding a push every poll while the issue persists.

```yaml
- id: wheresthebus_sensor_error
  alias: Where's the Bus sensor error alert
  description: Notify when the bus sensor cannot log in or retrieve data.
  triggers:
    - trigger: state
      entity_id: sensor.where_s_my_bus
      to: "error"
  actions:
    - action: notify.mobile_app_evan_5182218310
      data:
        title: Where's the Bus sensor needs attention
        message: >-
          The bus sensor reported an error ({{ state_attr('sensor.where_s_my_bus', 'error_type') or 'unknown error' }}).
          Check /config/wtb_credentials.json and the sensor diagnostics under /config/wtb_history/diagnostics/.
  mode: single
```

If your phone's notify service has a different name, replace `notify.mobile_app_evan_5182218310` with the service listed under Home Assistant Developer Tools → Actions. The automation triggers when the sensor changes into `error`; if the command-line sensor is `unavailable` instead, inspect its command and timeout configuration.
