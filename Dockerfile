FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner_full.py .

COPY dashboard_server.py .

COPY static/ ./static/

ENV MPLBACKEND=Agg

CMD ["python", "-u", "scanner_full.py"]
