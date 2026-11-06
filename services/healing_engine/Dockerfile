# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/healing_engine/Dockerfile -t aiops/healing-engine .
# docker-compose.aiops.yml already sets the context correctly.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install docker client library and psutil
RUN pip install --no-cache-dir docker==7.1.0 psutil==6.0.0

# Install shared library
COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common

# Copy healing engine code
COPY services/healing_engine/healing_engine /app/healing_engine

# Non-root user
RUN useradd --create-home --uid 10006 healer \
    && chown -R healer:healer /app
USER healer

ENV AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local \
    # Dry-run by default - must explicitly enable healing
    HEALING_ENABLED=false

EXPOSE 8000

# Health check to ensure the engine is running and can respond
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "healing_engine.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
