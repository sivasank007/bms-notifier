# Ticket Notifier — container image for Railway (or any Docker host).
#
# Two things drive this file:
#   * Cloudflare blocks headless browsers, so we install real Google Chrome
#     and run it headed inside a virtual framebuffer (xvfb). Nothing is drawn.
#   * Dates are compared against "today", so the timezone must be IST or an
#     alert for tomorrow can be rejected as being in the past.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    BMS_HOST=0.0.0.0 \
    BMS_HEADLESS=false \
    BMS_BROWSER_CHANNEL=chrome \
    BMS_PROFILE_DIR=/app/data/browser-profile

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb tzdata ca-certificates fonts-liberation \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chrome

COPY . .
RUN chmod +x docker-start.sh

EXPOSE 8000
CMD ["./docker-start.sh"]
