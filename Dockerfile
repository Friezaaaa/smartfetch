FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787 \
    HOST=0.0.0.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 smartfetch \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin smartfetch

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
 && python -m pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 smartfetch ./smartfetch
COPY --chown=10001:10001 tests/container_emergency_smoke.py ./tests/container_emergency_smoke.py

USER 10001:10001

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').getenv('PORT','8787') + '/health', timeout=3).read()"

CMD ["python", "-m", "smartfetch.server"]
