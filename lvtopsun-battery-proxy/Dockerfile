ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        bluez \
        bluez-deprecated \
        dbus \
        glib \
        python3

COPY run.sh /
COPY main.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
