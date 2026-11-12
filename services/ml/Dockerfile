FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/common \
    PROJECT_ROOT=/app \
    MODEL_DIR=/app/models \
    REPORTS_DIR=/app/reports \
    DATA_DIR=/app/data
WORKDIR /app
ARG INSTALL_EVIDENTLY=true

# 1) dependencies first -> cached layer, rebuilds are fast when only code changes
# (-c pins evidently to the same numpy/pandas/scikit-learn versions the API uses)
COPY services/ml/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && if [ "$INSTALL_EVIDENTLY" = "true" ]; then \
      pip install "evidently>=0.7,<0.8" -c /tmp/requirements.txt || echo "WARNING: evidently not installed - HTML drift reports disabled (PSI monitoring still works)"; \
    fi

# 2) shared code + service code
COPY common /app/common
COPY training /app/training
COPY services/ml /app/service

CMD ["python", "/app/service/monitor.py"]
