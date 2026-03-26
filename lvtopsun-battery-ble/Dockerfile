ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        python3 \
        py3-pip \
        bluez \
        dbus \
        glib \
    && pip3 install --no-cache-dir --break-system-packages \
        bleak==0.22.3 \
        paho-mqtt==2.1.0

COPY run.sh /
COPY main.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
