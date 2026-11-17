# Home Assistant base image with Python (Alpine). Pinned; current tags are listed in
# https://github.com/home-assistant/docker-base (README). The Supervisor no longer passes
# BUILD_FROM by default (since 2026.04), so it has a default here and build.yaml is not used.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

# The app is Python stdlib plus one pinned package: the official Anthropic SDK for the Claude writer
# (installed at build time; its compiled parts have musllinux wheels for amd64 and aarch64).
# No runtime downloads. tzdata: zoneinfo needs the IANA time zone database for call times and days.
#
# Signal (0.5.1, approved by the owner): signal-cli and its native libsignal from Alpine's community
# repository (x86_64 and aarch64), with a headless Java 25 runtime. Callsheet runs it as a read-only linked
# device (see app/signalcli.py). The community repository is on in the official Alpine image; the first
# line only adds it if a base image ever leaves it out. The build stops if libsignal's JNI library is missing
# or signal-cli can't start. Java stays small: 256 MB heap, serial garbage collector.
ENV JAVA_OPTS="-Xmx256m -XX:+UseSerialGC"
RUN grep -q '/community$' /etc/apk/repositories \
        || sed -n 's#/main$#/community#p' /etc/apk/repositories >> /etc/apk/repositories \
    && apk add --no-cache tzdata openjdk25-jre-headless 'signal-cli>=0.14.9' 'java-libsignal-client>=0.103.0' \
    && test -f /usr/lib/libsignal_jni.so && signal-cli --version \
    && pip install --no-cache-dir anthropic==2.3.0

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
