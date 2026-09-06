#!/usr/bin/with-contenv bashio

bashio::log.info "=== Stellantis Token Keeper ==="

mkdir -p /data/oauth_debug

# MQTT broker from the Supervisor (Mosquitto add-on), unless set in the options.
if bashio::config.has_value 'mqtt_host'; then
    bashio::log.info "Using MQTT broker from add-on options"
elif bashio::services.available 'mqtt'; then
    export TK_MQTT_HOST="$(bashio::services mqtt 'host')"
    export TK_MQTT_PORT="$(bashio::services mqtt 'port')"
    export TK_MQTT_USERNAME="$(bashio::services mqtt 'username')"
    export TK_MQTT_PASSWORD="$(bashio::services mqtt 'password')"
    bashio::log.info "Using MQTT broker provided by the Supervisor (${TK_MQTT_HOST}:${TK_MQTT_PORT})"
else
    bashio::log.warning "No MQTT broker available - status sensors are disabled"
fi

# Hostname under which Home Assistant Core reaches this add-on inside the hassio network.
export TK_ADDON_HOSTNAME="$(bashio::addon.hostname)"

cd /app
exec python3 -m main --options /data/options.json
