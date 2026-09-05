# SlopClanker application container.
# The Home Assistant add-on (flapperdeflipper/addons) builds on this image
# and adds run.sh + add-on labels; this image also runs standalone via
# docker-compose.yml.
FROM python:3.14-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app
WORKDIR /app

# SLOPCLANKER_TOKEN (bearer auth) and SLOPCLANKER_DB path come from the
# environment; defaults suit the compose file, the add-on overrides them.
ENV SLOPCLANKER_HOST=0.0.0.0 \
    SLOPCLANKER_PORT=8090 \
    SLOPCLANKER_DB=/data/slopclanker.db

VOLUME /data
EXPOSE 8090
CMD ["python3", "-m", "app.main"]
