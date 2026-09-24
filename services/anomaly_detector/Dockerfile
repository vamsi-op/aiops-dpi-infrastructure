# Build from the REPOSITORY ROOT, not this directory, because the image needs libs/:
#   docker build -f services/anomaly_detector/Dockerfile -t aiops/anomaly-detector .
# docker-compose.aiops.yml already sets the context correctly.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The ML stack installs first and in its own layer. scikit-learn, scipy and numpy are ~100 MB
# of wheels that change far less often than the detector code, so an edit to a scoring rule
# must not trigger a reinstall of all three.
#
# requirements-ml.txt rather than requirements.txt: the latter also carries the editable
# `-e ../../libs/aiops_common` line, whose relative path does not exist in the image. Keeping
# the pins in one file means the image and a local venv install the same versions.
COPY services/anomaly_detector/requirements-ml.txt /tmp/requirements-ml.txt
RUN pip install --no-cache-dir -r /tmp/requirements-ml.txt

COPY libs/aiops_common /app/libs/aiops_common
RUN pip install --no-cache-dir /app/libs/aiops_common

COPY services/anomaly_detector/anomaly_detector /app/anomaly_detector

# Model artifacts are written here and bind-mounted from the host, so a retrain survives a
# container rebuild and can be inspected without exec'ing in.
RUN mkdir -p /app/ml/artifacts

# Non-root, and it owns the artifact directory because training writes joblib files there.
RUN useradd --create-home --uid 10003 detector \
    && chown -R detector:detector /app
USER detector

ENV AIOPS_LOG_FORMAT=json \
    AIOPS_ENV=local \
    MODEL_ARTIFACT_DIR=/app/ml/artifacts \
    # scikit-learn and numpy each spin up a thread pool sized to the host's CPU count. Inside a
    # container with a fractional CPU limit that causes heavy contention and makes scoring
    # slower, not faster. One thread is right for scoring a single nine-feature vector.
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

EXPOSE 8000

# A detector that has silently stopped consuming is exactly the failure worth restarting, and
# module 9 never injects faults into the AIOps services themselves, so there is no remediation
# behaviour for an auto-restart to mask.
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "anomaly_detector.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
