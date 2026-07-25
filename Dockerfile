FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY src/steam_skin_ops/__init__.py ./src/steam_skin_ops/__init__.py
COPY src/steam_skin_ops/monitor ./src/steam_skin_ops/monitor
RUN pip install --no-cache-dir ".[monitor]" \
    && mkdir -p /app/data/backups \
    && useradd --system --uid 10001 --home /app appuser \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "steam_skin_ops.monitor.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
