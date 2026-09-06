## 0.1.1

- `email` and `password` options may be empty, so the add-on starts as a plain login
  worker before credentials are entered

## 0.1.0

First version.

- Login worker on port 3000, API-compatible with the upstream Render.com service
  (`POST /` → `{"code"}`), full Chromium in the new headless mode
- Watchdog: periodic reload probe, detection of `setup_error` / pending reauth flow,
  automatic completion of the integration's reauth flow via the Home Assistant API
- Status as MQTT discovery entities (problem, last check, last reauth, last login, message)
  and persistent notification on failure
- Browser login derived from taubenhorst/StellantisHAAddon (MIT)
