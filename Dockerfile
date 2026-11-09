FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY pyproject.toml ./

RUN mkdir -p /app/data && useradd -r -u 10001 ghostwriter && chown -R ghostwriter /app
USER ghostwriter

EXPOSE 8080
CMD ["python", "-m", "app.main"]
