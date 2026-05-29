FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "monitor.py"]
