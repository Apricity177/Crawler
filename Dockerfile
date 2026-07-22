FROM python:3.12-slim

WORKDIR /app

COPY . /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DAILY_AT=08:30 \
    DATA_DIR=/app/data \
    CRAWLER_CONFIG=/app/config.example.json

RUN mkdir -p /app/data && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
