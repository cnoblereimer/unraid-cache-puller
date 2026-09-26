FROM python:3.12-slim

# tini: proper PID 1 signal handling; tzdata: so TZ (and ALLOWED_HOURS) work.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/build

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    UI_PORT=8080

VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('UI_PORT','8080'), timeout=4)" || exit 1

# -s: register tini as a child subreaper. With --pid=host (which this container
# needs) tini is not PID 1, and without -s it can't reap zombie processes.
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "cache-puller"]
CMD ["run"]
