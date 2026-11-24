FROM python:3.12-slim

ARG PAYLOAD_DUMPER_VERSION=2.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 ffmpeg megatools xz-utils ca-certificates curl \
    && curl -fsSL \
        "https://github.com/ssut/payload-dumper-go/releases/download/${PAYLOAD_DUMPER_VERSION}/payload-dumper-go_${PAYLOAD_DUMPER_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin payload-dumper-go \
    && chmod +x /usr/local/bin/payload-dumper-go \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data/downloads \
    && chown -R bot:bot /app /data

USER bot

ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/data/downloads \
    STATE_FILE=/data/state/telegram.json

CMD ["python", "-m", "bot"]
