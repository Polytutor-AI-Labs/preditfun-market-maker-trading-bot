FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY predict_mm ./predict_mm

RUN pip install --no-cache-dir .

CMD ["python", "-m", "predict_mm.main", "--config", "config.toml"]
