FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

RUN pip install --no-cache-dir playwright tapo python-kasa tzdata
RUN playwright install chromium
RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

COPY monitor.py .

ARG APP_VERSION=unknown
ENV APP_VERSION=${APP_VERSION}

VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-u", "monitor.py"]
