ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        bluez \
        dbus \
        glib \
        python3 \
        py3-pip \
    && pip3 install --no-cache-dir --break-system-packages \
        bleak==0.22.3 \
        dbus-next==0.2.3 \
        paho-mqtt==2.1.0

COPY run.sh /
COPY main.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
