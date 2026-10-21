FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 PORT=8000 DATABASE_PATH=/data/outreach-wise.db

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY outreach_wise ./outreach_wise
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /app outreach \
 && mkdir -p /data && chown outreach /data

USER outreach
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["outreach-wise", "serve"]
