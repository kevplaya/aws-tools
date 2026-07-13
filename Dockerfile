FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8501 \
    AWS_DASHBOARD_DB=/data/aws-tools.db

WORKDIR /app

COPY requirements-dashboard.txt ./
RUN pip install --no-cache-dir --requirement requirements-dashboard.txt

COPY aws_audit.py dashboard.py ./
COPY dashboard_app ./dashboard_app

EXPOSE 8501

CMD ["gunicorn", "--bind", "0.0.0.0:8501", "--workers", "1", "--threads", "2", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "dashboard:app"]
