# Reserve Juggler AI — production image. Data (accounts, audit chain, datasets, model cache) lives in /data.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RJ_HOST=0.0.0.0 RJ_PORT=8000 RJ_DATA_DIR=/data RJ_DEMO_CSV=/app/data/treasury_4tier_dataset.csv

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY tools ./tools
COPY data/treasury_4tier_dataset.csv ./data/treasury_4tier_dataset.csv

RUN useradd --create-home --uid 10001 rj && mkdir -p /data && chown rj:rj /data
USER rj
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "backend/app.py"]
