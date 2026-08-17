# Base image ships with Intel QSV, VAAPI and iHD drivers pre-built for FFmpeg.
# This is the recommended path for Intel Alder Lake-N (N100/N200/N305/8505) iGPUs.
FROM lscr.io/linuxserver/ffmpeg:latest

# The linuxserver image sets ENTRYPOINT to ffmpeg. Override it so we can run Python.
ENTRYPOINT []

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-yaml \
        tini \
        ca-certificates \
        curl \
        intel-gpu-tools \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin — needed so the "Save & Restart" button can
# rewrite docker-compose.yml and recreate this container via the socket.
ARG DOCKER_VERSION=24.0.9
ARG COMPOSE_VERSION=v2.29.7
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
        -o /tmp/docker.tgz \
    && tar xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm /tmp/docker.tgz \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# FastAPI + uvicorn for the web UI; ruamel.yaml preserves comments when the
# UI writes config.yaml back. Pinned to avoid surprise upgrades.
RUN pip3 install --no-cache-dir --break-system-packages \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.1" \
        python-multipart==0.0.19 \
        ruamel.yaml==0.18.6

WORKDIR /app
COPY app/ /app/

RUN mkdir -p /config /logs /state /tmp/convert

# tini reaps zombie ffmpeg processes cleanly
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-u", "/app/convert.py"]
