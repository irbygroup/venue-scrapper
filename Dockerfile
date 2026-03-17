FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries
RUN playwright install chromium

# Install cron + set timezone to Central
RUN apt-get update && apt-get install -y cron tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=America/Chicago
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy app
COPY api.py schema_pg.sql entrypoint.sh ./
COPY app/ app/
COPY prompts/ prompts/
RUN chmod +x entrypoint.sh

# Cron jobs (times in America/Chicago)
# Lead Market: every 30 min at :22/:52, Mon-Sat, 8am-6pm Central (moves free leads to inbox + triggers sync)
# Sync: every 2h at :32, Mon-Sat, 8am-7pm Central
# Drip: 10 min after each sync (:42) to process any new campaigns
# Daily report: 7am Central
# FUB webhook ensure: hourly
RUN printf '%s\n' \
    '22,52 8,10,12,14,16,18 * * 1-6 curl -s -X POST http://localhost:5050/eventective/check-lead-market >> /var/log/cron.log 2>&1' \
    '0 7 * * * curl -s http://localhost:5050/eventective/daily_report >> /var/log/cron.log 2>&1' \
    '15 * * * * curl -s http://localhost:5050/eventective/fub-webhook/ensure >> /var/log/cron.log 2>&1' \
    '32 8,10,12,14,16,18 * * 1-6 curl -s -X POST http://localhost:5050/eventective/sync >> /var/log/cron.log 2>&1' \
    '42 8,10,12,14,16,18 * * 1-6 curl -s -X POST http://localhost:5050/eventective/drip/process >> /var/log/cron.log 2>&1' \
    > /etc/cron.d/venue-scrapper \
    && chmod 0644 /etc/cron.d/venue-scrapper \
    && crontab /etc/cron.d/venue-scrapper

CMD ["./entrypoint.sh"]
