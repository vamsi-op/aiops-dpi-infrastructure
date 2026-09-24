# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/traffic_predictor/Dockerfile -t aiops/traffic-predictor .
# docker-compose.aiops.yml already sets the context correctly.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No ML wheel layer here, unlike the anomaly detector: the forecaster is medians and arithmetic
# from the standard library, so the shared library is the only dependency.
COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common

COPY services/traffic_predictor/traffic_predictor /app/traffic_predictor

RUN useradd --create-home --uid 10004 predictor && chown -R predictor:predictor /app
USER predictor

ENV AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local

EXPOSE 8000

# A predictor that has silently stopped forecasting is worth restarting, and module 9 never
# injects faults into the AIOps services themselves, so there is no remediation behaviour for an
# auto-restart to mask.
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "traffic_predictor.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
