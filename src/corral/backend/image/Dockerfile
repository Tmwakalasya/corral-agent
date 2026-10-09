# corral cell image. Used for both the sandbox and its egress proxy.
# Base pinned by digest so a tag move upstream can't change what runs here.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl file git jq less make procps ripgrep tree \
      nodejs npm build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.10.4

RUN groupadd -g 1000 agent \
 && useradd -m -u 1000 -g 1000 -s /bin/bash agent \
 && mkdir -p /workspace \
 && chown agent:agent /workspace

COPY proxy.py fsops.py /opt/corral/
RUN chmod 0444 /opt/corral/*.py

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    npm_config_update_notifier=false

WORKDIR /workspace
USER agent
