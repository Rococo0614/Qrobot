FROM python:3.12-slim

ARG QROBOT_EXTRAS="onebot,search"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app/data/home

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[${QROBOT_EXTRAS}]" \
    && useradd --system --uid 10001 --create-home --home-dir /app/data/home qrobot \
    && mkdir -p /app/data \
    && chown -R qrobot:qrobot /app

USER qrobot
VOLUME ["/app/data"]
CMD ["python", "-m", "qrobot", "run"]
