# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/api_gateway/Dockerfile -t aiops/api-gateway .
# docker-compose.aiops.yml already sets the context correctly.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Shared library
COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common kubernetes==33.1.0

# Application code
COPY services/api_gateway/api_gateway /app/api_gateway

RUN useradd --create-home --uid 10005 gateway && chown -R gateway:gateway /app
USER gateway

ENV AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "api_gateway.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
