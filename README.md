# Stellantis Token Keeper

Home Assistant add-on for users of the HACS integration
[homeassistant-stellantis-vehicles](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles)
(Peugeot, Citroën, DS, Opel, Vauxhall). It does two things:

1. **Self-hosted login worker.** The integration's "automatic" OAuth mode posts your
   e-mail and password to a login service that drives the Stellantis web login in a
   headless browser and returns the OAuth code. The public service runs on a free
   Render.com instance and is frequently unavailable. This add-on provides the same
   HTTP API on your own Home Assistant host: enter `http://<ha-ip>:3000` as
   *Login service URL* in the integration's re-authentication dialog.

2. **Watchdog with automatic re-authentication.** Stellantis invalidates the refresh
   token roughly every week. The integration then silently keeps showing stale data
   until it is reloaded. The add-on reloads the config entry periodically, detects a
   failed token, and completes the integration's re-authentication flow on its own,
   using the credentials from the add-on options. The result is published as MQTT
   sensors, so you can build notifications on top.

Your credentials stay on your host. Nothing is sent anywhere except to Stellantis'
own login pages.

## Installation

Settings → Add-ons → Add-on Store → ⋮ → Repositories →
`https://github.com/MarkusReuter/ha-stellantis-token-keeper`

Then install *Stellantis Token Keeper*, fill in e-mail and password, start it.
The first build downloads Chromium and takes a few minutes.

See `stellantis_token_keeper/DOCS.md` for options, the MQTT entities and how the
re-authentication flow is driven.

## Disclaimer

Unofficial community project, not affiliated with or endorsed by Stellantis.
Uses the same non-public login pages as the upstream integration, which may change
at any time. MIT licensed; the browser login is derived from
[taubenhorst/StellantisHAAddon](https://github.com/taubenhorst/StellantisHAAddon) (MIT).
