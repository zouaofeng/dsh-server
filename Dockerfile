FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# bubblewrap: sandbox backend of DSH_PERMISSION_MODE=workspace-write
# git / curl / procps: everyday tools for the agent's bash tool
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl git procps \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user (uid/gid can be changed at run time with APP_UID / APP_GID)
RUN groupadd -g 1000 app \
    && useradd -u 1000 -g app -d /home/app -m -s /bin/bash app \
    && chmod 1777 /home/app
ENV HOME=/home/app

WORKDIR /app

# Optional PyPI mirror, e.g. --build-arg PIP_MIRROR=https://mirrors.tencent.com/pypi/simple/
ARG PIP_MIRROR=""
COPY requirements.txt .
RUN pip install ${PIP_MIRROR:+-i "$PIP_MIRROR"} -r requirements.txt

COPY app ./app
COPY patches ./patches
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV DSH_HOME=/app/data/home \
    DSH_WORKSPACE=/app/data/workspace

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz >/dev/null || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
# The harness lives in process memory: always a single worker.
# On SIGTERM, in-flight agent runs get 10 s before they are cancelled; then the runtime is
# closed (up to 15 s). Keep the compose stop_grace_period above 25 s.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "10"]
