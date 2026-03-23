FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        dosfstools \
        e2fsprogs \
        mount \
        parted \
        udisks2 \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app /app

EXPOSE 8080

CMD ["python3", "/app/server.py"]
