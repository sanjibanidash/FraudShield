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

# 1) dependencies first -> cached layer, rebuilds are fast when only code changes
COPY services/producer/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# 2) shared code + service code
COPY common /app/common
COPY services/producer /app/service

# run as an unprivileged user
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8002

CMD ["python", "/app/service/producer.py"]
