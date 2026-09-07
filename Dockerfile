FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir "poetry>=2.0,<3.0"

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --no-interaction --no-ansi

# Playwright's headless Chromium (spec section 2's PDF export) needs real
# system libraries (libnss3, libatk, etc.), not just the pip package -
# --with-deps installs those via apt. This is the actual reason this app
# needs a Docker-based deploy rather than a plain Python buildpack (Render's
# auto-detected Python runtime has no apt step to hook this into).
RUN playwright install --with-deps chromium

COPY . .

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
