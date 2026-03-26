ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        bluez \
        dbus \
        glib \
    && pip install --no-cache-dir \
        bleak==0.22.3 \
        paho-mqtt==2.1.0

COPY run.sh /
COPY main.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
