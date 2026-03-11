FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries
RUN playwright install chromium

# Copy app
COPY api.py scrape_leads.py ./

EXPOSE 5050

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "5050", "--log-level", "info"]
