FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries
RUN playwright install chromium

# Install cron
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

# Copy app
COPY api.py schema_pg.sql entrypoint.sh ./
COPY app/ app/
RUN chmod +x entrypoint.sh

# Cron jobs
RUN printf '%s\n' \
    '0 12 * * * curl -s http://localhost:5050/eventective/daily_report >> /var/log/cron.log 2>&1' \
    '15 * * * * curl -s http://localhost:5050/eventective/fub-webhook/ensure >> /var/log/cron.log 2>&1' \
    > /etc/cron.d/venue-scrapper \
    && chmod 0644 /etc/cron.d/venue-scrapper \
    && crontab /etc/cron.d/venue-scrapper

CMD ["./entrypoint.sh"]
