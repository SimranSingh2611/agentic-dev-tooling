FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY . .
RUN pip install --no-deps . \
    && groupadd --gid 10001 application \
    && useradd --uid 10001 --gid application --no-create-home application \
    && mkdir -p /data /workspaces \
    && chown -R application:application /data /workspaces

USER 10001:10001
CMD ["uvicorn", "shortener.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
