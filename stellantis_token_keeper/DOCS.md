# Stellantis Token Keeper

Companion add-on for the HACS integration
[homeassistant-stellantis-vehicles](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles).
It hosts the OAuth login worker on your own Home Assistant and keeps the integration
authenticated automatically.

## Background

Stellantis invalidates the integration's refresh token roughly once a week
(`invalid_grant`). The integration refreshes tokens in a timer job that does not handle
this error: the job dies silently, no re-authentication is requested, and all entities
keep their last values until the integration is reloaded. After a reload the entry goes
to `setup_error` and Home Assistant shows the re-authentication dialog. The dialog's
"automatic" mode posts your credentials to a login service (default: a shared free
Render.com instance that is often down); the "manual" mode requires fishing the OAuth
code out of the browser's developer tools.

## What the add-on does

**Login worker** (port 3000): `POST /` with `{"url", "email", "password"}` opens the
Stellantis login page in a headless Chromium, signs in, confirms the consent form and
returns `{"code": "…"}`, exactly like the upstream worker. Enter
`http://<ha-ip>:3000` as *Login service URL* in the integration's dialog and the
automatic mode works from any device, including the phone.

**Watchdog**: every `check_interval_hours`

1. reloads the config entry (`reload_probe`), which makes an invalid token visible
   immediately instead of hours later,
2. if the entry is in `setup_error` or a re-authentication flow is pending, completes
   that flow through the Home Assistant API: automatic mode, own worker URL,
   credentials from the options, remote commands as configured,
3. publishes the result as MQTT entities and, on failure, as a persistent notification.

## Options

| Option | Default | Meaning |
|---|---|---|
| `email`, `password` | | Stellantis account (MyPeugeot etc.). Used only for the browser login on this host. |
| `check_interval_hours` | 4 | Watchdog interval. |
| `reload_probe` | true | Reload the entry on every check to surface a dead token. Costs a few seconds of unavailability per check. |
| `remote_commands` | false | Value submitted in the "remote commands" step. `true` leads to the OTP step (SMS + PIN) which cannot be automated; the flow then stays open for you to finish in the UI. |
| `login_timeout_seconds` | 90 | Timeout for one browser login. |
| `log_level` | info | `debug` logs every URL the browser sees (query values masked). |
| `worker_url` | auto | URL Home Assistant Core uses to reach the worker. Defaults to the add-on's internal hostname. |
| `ha_url`, `ha_token` | Supervisor | Override for development outside the Supervisor. |
| `mqtt_*` | Supervisor | Override the broker; by default the Mosquitto add-on's credentials are used. |

## Entities (MQTT discovery)

Device *Stellantis Token Keeper*:

| Entity (IDs follow the German names) | Content |
|---|---|
| `binary_sensor.stellantis_token_keeper_problem` | `on` when the last check failed or re-authentication did not succeed |
| `sensor.stellantis_token_keeper_letzte_prufung` | time of the last watchdog check |
| `sensor.stellantis_token_keeper_letzte_neuanmeldung` | time of the last successful automatic re-authentication |
| `sensor.stellantis_token_keeper_letzter_browser_login` | time of the last successful browser login |
| `sensor.stellantis_token_keeper_letzte_meldung` | last result as text, full status as attributes |

Build your notification automation on the `problem` binary sensor.

## HTTP API

| Route | Purpose |
|---|---|
| `POST /` | login worker, upstream-compatible |
| `GET /health` | liveness |
| `GET /status` | status JSON (same content as the MQTT state) |
| `POST /check` | run a watchdog check now |

## Troubleshooting

- **Login fails with "IdP rejected the login"**: wrong credentials, or Stellantis asks
  for something new on the login page. With `log_level: debug` a screenshot and the page
  source of the last page are written to `/data/oauth_debug/`.
- **"cannot be completed automatically" at step `otp`**: `remote_commands` is `true`.
  Finish the SMS step in Settings → Integrations, or set the option to `false`.
- **Watchdog reports "not an auth problem"**: the entry failed for another reason
  (Stellantis backend down, network). Nothing is changed; check the integration's log.
- **Entities missing**: the Mosquitto add-on and the MQTT integration must be set up;
  otherwise the add-on logs a warning and runs without status entities.
