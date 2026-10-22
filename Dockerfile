ARG PYTHON_IMAGE=python:3.14-slim-bookworm@sha256:f8417a152ae83c696bee8861ed715acd93a9c9a925cd868325bc0afd788537e6
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_ENV=production \
    STOCK_ALERT_CONTAINER=true \
    DATABASE_URL=/app/data/stock-alert.db
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --disable-pip-version-check -r requirements.txt \
    && groupadd --gid 1000 node \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin node \
    && mkdir -p /app/data && chown node:node /app/data
COPY trade_alert ./trade_alert
COPY migrations ./migrations
USER node
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-m", "trade_alert.health"]
CMD ["python", "-m", "trade_alert"]
