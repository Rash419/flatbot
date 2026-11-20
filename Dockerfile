# Codex CLI version (npm package @openai/codex).
ARG CODEX_VERSION=0.171.0

# --- Stage 1: fetch the Codex CLI with npm and keep only its native binary -------------
FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION
WORKDIR /tmp/codex
# npm installs the platform package for the architecture being built (amd64 or arm64).
# It holds a static musl binary at vendor/<triple>/bin/codex beside codex-path/ and
# codex-resources/ (bwrap, rg); the JS launcher is not needed to run it.
RUN npm install --no-save --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
    && set -- node_modules/@openai/codex-linux-*/vendor/*-unknown-linux-musl \
    && [ "$#" -eq 1 ] && [ -x "$1/bin/codex" ] \
    && cp -a "$1" /opt/codex \
    && /opt/codex/bin/codex --version

# --- Stage 2: the bot -------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/bot.db \
    CODEX_HOME=/data/codex \
    PORT=8080 \
    PATH=/opt/codex/bin:$PATH

# The bot runs as this unprivileged user; the entrypoint starts as root only long
# enough to make /data writable for it (see docker-entrypoint.sh).
RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/codex && chown -R app:app /data && chmod 700 /data/codex

COPY --from=codex /opt/codex /opt/codex

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY app ./app

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
