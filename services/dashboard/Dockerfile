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
COPY services/dashboard/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# 2) shared code + service code
COPY common /app/common
COPY services/dashboard /app/service
WORKDIR /app/service

# run as an unprivileged user
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 5000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=4)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--access-logfile", "-", "app:app"]
