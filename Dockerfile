FROM python:3.12-slim

ARG PAYLOAD_DUMPER_VERSION=2.1.0

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        aria2 ffmpeg megatools xz-utils ca-certificates curl 7zip; \
    case "$(dpkg --print-architecture)" in \
        amd64) payload_arch=amd64 ;; \
        arm64) payload_arch=arm64 ;; \
        *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl --connect-timeout 15 --max-time 180 -fsSL \
        -o /tmp/payload-dumper-go.tar.gz \
        "https://github.com/ssut/payload-dumper-go/releases/download/${PAYLOAD_DUMPER_VERSION}/payload-dumper-go_${PAYLOAD_DUMPER_VERSION}_linux_${payload_arch}.tar.gz"; \
    tar -xzf /tmp/payload-dumper-go.tar.gz \
        -C /usr/local/bin payload-dumper-go; \
    chmod +x /usr/local/bin/payload-dumper-go; \
    rm -f /tmp/payload-dumper-go.tar.gz; \
    rm -rf /var/lib/apt/lists/*

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
