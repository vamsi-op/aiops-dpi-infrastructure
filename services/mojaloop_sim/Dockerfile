# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/mojaloop_sim/Dockerfile -t aiops/mojaloop-sim .
# docker-compose.observability.yml already sets the context correctly.

FROM python:3.12-slim AS base

# Python behaviour suited to containers: no .pyc files, unbuffered stdout so logs reach
# Docker (and therefore Alloy, and therefore Loki) immediately rather than sitting in a
# buffer. Buffered output would also break the log timestamps module 7 orders events by.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the simulator
# code, so edits to the source do not trigger a full reinstall.
COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common

# The ASGI app lives inside the package (mojaloop_sim/asgi.py), so copying the package is
# all that is needed - there is no top-level entry-point module.
COPY services/mojaloop_sim/mojaloop_sim /app/mojaloop_sim

# Non-root. The container has no reason to run privileged, and module 9 will be terminating
# these processes deliberately - better that it cannot do anything else.
RUN useradd --create-home --uid 10001 simuser && chown -R simuser:simuser /app
USER simuser

# Defaults; docker-compose overrides SIM_SERVICE per container.
ENV SIM_SERVICE=central-ledger \
    AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local

EXPOSE 8000

# No Docker HEALTHCHECK on purpose. Compose would restart an unhealthy container, which
# would silently repair exactly the failures module 9 injects and module 6 is supposed to
# remediate. Liveness is exposed at /health for the platform to judge instead.

CMD ["uvicorn", "mojaloop_sim.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
