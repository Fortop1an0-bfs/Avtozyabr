FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy source before install so editable link resolves correctly
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e ".[dev]" \
    && playwright install chromium --with-deps

CMD ["python", "-m", "avtozyabr.main"]
