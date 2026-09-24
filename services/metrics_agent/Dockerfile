# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/metrics_agent/Dockerfile -t aiops/metrics-agent .
# docker-compose.aiops.yml already sets the context correctly.

FROM python:3.12-slim AS base

# No .pyc files, and unbuffered stdout so logs reach Docker (and therefore Alloy, and
# therefore Loki) immediately rather than sitting in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the agent code,
# so edits to the source do not trigger a full reinstall.
COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common

COPY services/metrics_agent/metrics_agent /app/metrics_agent

# Non-root. The agent reads Prometheus and writes Postgres and Redis over the network; it
# has no reason to touch the host.
RUN useradd --create-home --uid 10002 agentuser && chown -R agentuser:agentuser /app
USER agentuser

ENV AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local

EXPOSE 8000

# Unlike the simulators, this one does get a HEALTHCHECK. Module 9 never injects failures
# into the AIOps services themselves, so there is no remediation behaviour for an
# auto-restart to mask - and an agent that has silently stopped collecting is exactly the
# failure worth restarting.
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "metrics_agent.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
