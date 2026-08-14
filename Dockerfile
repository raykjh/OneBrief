FROM node:24-bookworm-slim AS node_runtime

FROM python:3.12-slim

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        git && \
    ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10001 onebrief

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip && \
    python -m pip install ".[dev]"

USER 10001:10001

ENTRYPOINT ["onebrief", "cloud-worker"]
