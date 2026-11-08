FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium
COPY . .
RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1
CMD ["sh", "/app/start.sh"]
