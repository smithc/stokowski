FROM python:3.12-slim

# Install Docker CLI (not daemon — uses host daemon via socket)
RUN apt-get update && \
    apt-get install -y --no-install-recommends docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY stokowski/ stokowski/
RUN pip install --no-cache-dir ".[web]"

ENTRYPOINT ["stokowski"]
