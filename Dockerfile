FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements-lock.txt ./
RUN pip install -r requirements-lock.txt
COPY app ./app
COPY deploy/media-tool /opt/media-tools/ffmpeg
COPY deploy/media-tool /opt/media-tools/ffprobe
COPY deploy/start.sh /app/start.sh
RUN chmod 755 /opt/media-tools/ffmpeg /opt/media-tools/ffprobe /app/start.sh \
    && useradd --uid 10001 --create-home clipdrop \
    && mkdir /app/.data && chown clipdrop:clipdrop /app/.data
USER clipdrop
EXPOSE 10000
CMD ["/app/start.sh"]
