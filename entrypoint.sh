#!/bin/bash
# Start cron daemon in background
cron

# Start the API
exec uvicorn api:app --host 0.0.0.0 --port 5050 --log-level info
