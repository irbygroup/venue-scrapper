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
COPY api.py scrape_leads.py schema_pg.sql entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Daily report cron — 7 AM Central (12:00 UTC)
RUN echo '0 12 * * * curl -s http://localhost:5050/eventective/daily_report >> /var/log/cron.log 2>&1' > /etc/cron.d/daily-report \
    && chmod 0644 /etc/cron.d/daily-report \
    && crontab /etc/cron.d/daily-report

CMD ["./entrypoint.sh"]
