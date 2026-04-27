FROM python:3.12-slim

# System deps needed by playwright and browser-cookie3
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" \
    && playwright install chromium --with-deps

COPY src/ src/

CMD ["python", "-m", "avtozyabr.main"]
