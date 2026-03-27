ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        bluez \
        bluez-deprecated \
        dbus \
        glib \
        python3 \
        py3-pip \
    && apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev \
        python3-dev \
    && pip3 install --no-cache-dir --break-system-packages \
        bleak \
        bless \
    && apk del .build-deps

COPY run.sh /
COPY main.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
