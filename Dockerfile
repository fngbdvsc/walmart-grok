FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium
COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x768x24", "python", "walmart_monitor.py"]
