FROM python:3.13-alpine

# One image, two roles: ROLE=relay (tailnet-only, Serve) or ROLE=inbox (public, Funnel).
ENV PYTHONUNBUFFERED=1 ROLE=relay DATA_DIR=/data
WORKDIR /app
COPY lightrelay ./lightrelay

# Unraid's usual owner for appdata: nobody:users (99:100).
RUN adduser -D -H -u 99 -G users relay && mkdir -p /data /inbox /outbox && chown relay:users /data
USER relay
VOLUME ["/data"]
EXPOSE 8080 8081

CMD ["python", "-m", "lightrelay"]
