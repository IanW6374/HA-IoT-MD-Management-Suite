ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache nginx
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt
COPY rootfs /
RUN chmod a+x /etc/cont-init.d/10-iotmd-config \
    /etc/services.d/iotmd-management/run /etc/services.d/nginx/run
