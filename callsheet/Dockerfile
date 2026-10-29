# Home Assistant base image with Python (Alpine). Pinned; current tags are listed in
# https://github.com/home-assistant/docker-base (README). The Supervisor no longer passes
# BUILD_FROM by default (since 2026.04), so it has a default here and build.yaml is not used.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

# The app is Python stdlib plus one pinned package: the official Anthropic SDK for the Claude writer
# (installed at build time; its compiled parts have musllinux wheels for amd64 and aarch64).
# No runtime downloads. tzdata: zoneinfo needs the IANA time zone database for call times and days.
RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir anthropic==2.3.0

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
