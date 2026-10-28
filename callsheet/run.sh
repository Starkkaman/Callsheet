#!/command/with-contenv bashio
# Starts the Callsheet web server for Home Assistant Ingress (port 8099, see config.yaml).
# server.py only accepts the Supervisor's ingress proxy (172.30.32.2) and loopback.
bashio::log.info "Starting Callsheet (step 1: sample data, nothing is sent)"
exec python3 -u /app/server.py --host 0.0.0.0 --port 8099
