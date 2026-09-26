FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/build

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config

VOLUME /config
ENTRYPOINT ["/usr/bin/tini", "--", "cache-puller"]
CMD ["run"]
