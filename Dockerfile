FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user for the app
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Dependencies first so this layer is cached between code changes.
# psycopg-binary and Pillow ship as wheels, so no compiler is needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# migrate + collectstatic, then gunicorn (see entrypoint.sh)
ENTRYPOINT ["/app/entrypoint.sh"]
