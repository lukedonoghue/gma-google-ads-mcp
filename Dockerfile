# The upstream image is intentionally small. Dependencies are pinned in
# pyproject.toml/uv.lock so a rebuild cannot silently change OAuth behavior.
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set the working directory in the container
WORKDIR /app

# Install the locked runtime first so source-only changes can reuse this layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 65532:65532

# Expose port 8080 (default for Cloud Run)
EXPOSE 8080

# Define the command to run the server
# This uses the entry point defined in pyproject.toml
CMD ["google-ads-mcp"]
