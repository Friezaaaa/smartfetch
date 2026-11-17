FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROMIUM_PATH=/usr/bin/chromium \
    CHROMIUM_NO_SANDBOX=true \
    BROWSER_WORKER_REQUIRED=false \
    X402_ENABLED=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-freefont-ttf \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r apify && useradd -r -g apify -d /home/apify -m apify

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY deploy/apify/requirements-apify.txt /app/requirements-apify.txt

RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-apify.txt

COPY smartfetch /app/smartfetch
COPY .actor /app/.actor

RUN chown -R apify:apify /app

USER apify

CMD ["python", "-m", "smartfetch.apify_actor"]
