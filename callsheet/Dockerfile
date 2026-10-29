# Home Assistant base image with Python (Alpine). Pinned; current tags are listed in
# https://github.com/home-assistant/docker-base (README). The Supervisor no longer passes
# BUILD_FROM by default (since 2026.04), so it has a default here and build.yaml is not used.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

# The app is Python stdlib only: no pip install, no runtime downloads.
# tzdata: zoneinfo needs the IANA time zone database for call times and calendar days.
RUN apk add --no-cache tzdata

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
