# Allowlist HTTPS proxy for sandboxes. Only hosts in `allowlist` can be reached.
FROM alpine:3.22
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
ARG ALLOWLIST=allowlist
COPY ${ALLOWLIST} /etc/tinyproxy/allowlist
EXPOSE 3128
USER tinyproxy
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
