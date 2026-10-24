# ================================================================
# Multi-Stage Dockerfile for Production
# ================================================================

# ----------------------------------------------------------------
# Stage 1: Builder - Install Dependencies with Poetry
# ----------------------------------------------------------------
FROM python:3.11-slim AS builder

# Set environment variables for build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

WORKDIR /build

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 - && \
    ln -s /root/.local/bin/poetry /usr/local/bin/poetry

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Install dependencies (ohne dev dependencies, ohne das Projekt selbst)
# --only main installiert nur Produktions-Dependencies
RUN poetry install --no-root --no-interaction --no-ansi --only main 2>/dev/null || \
    poetry install --no-root --no-interaction --no-ansi

# ----------------------------------------------------------------
# Stage 2: Runtime - Production Image
# ----------------------------------------------------------------
FROM python:3.11-slim

# Metadata
LABEL maintainer="your.email@example.com" \
      version="1.0.0" \
      description="FastAPI Backend with Auth, Git & Celery"

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/app/.venv/bin:$PATH \
    APP_HOME=/app

# Install runtime system dependencies. ``apt-get upgrade`` runs first so
# the base ``python:3.11-slim`` tag picks up Debian-security backports
# released after the upstream image was last rebuilt — that's where
# things like CVE-2026-45447 (openssl 3.5.6-1~deb13u2) come from. Trivy
# blocks the push on any HIGH/CRITICAL OS finding, so even though it
# enlarges the layer slightly we'd rather take the bytes than burn a
# .trivyignore line every time upstream debian releases a CVE-fix.
#
# CI passes a new APT_REFRESH on every run, so this layer never comes
# from the build cache. A cached layer keeps the packages of the day it
# was first built, and Trivy then blocks on a fix that is already
# released (libbrotli1, CVE-2026-111497, 24.10.2026).
ARG APT_REFRESH=local
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    postgresql-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Upgrade base-image Python tooling (pip / setuptools / wheel) to pull
# in security fixes that the upstream `python:3.11-slim` tag hasn't
# picked up yet. Trivy scans these system site-packages — anything
# HIGH/CRITICAL here blocks the push.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Create application directory
WORKDIR $APP_HOME

# Copy virtual environment from builder
COPY --from=builder /build/.venv /app/.venv

# Drop pip from both interpreters. It ships a CycloneDX SBOM of its vendored
# libraries at `pip/_vendor/bom.cdx.json`, and Trivy reads that as if those
# libraries were installed — reporting msgpack and setuptools versions that
# exist only inside pip and are never executed. Upgrading pip does not help;
# those are the versions current pip vendors.
#
# A runtime image installs nothing, so this removes the code rather than
# silencing the report. Both paths are spelled out because PATH puts the venv
# first, so a bare `python` would only ever reach one of the two.
RUN /app/.venv/bin/python -m pip uninstall -y pip && \
    /usr/local/bin/python -m pip uninstall -y pip

# Copy application code
COPY --chown=appuser:appuser . .

# Create necessary directories with proper permissions
RUN mkdir -p /tmp/repos && \
    chown -R appuser:appuser /tmp/repos && \
    chown -R appuser:appuser $APP_HOME

# Switch to non-root user
USER appuser

# Expose application port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default environment variables (can be overridden)
ENV DEBUG=False \
    PORT=8000 \
    HOST=0.0.0.0 \
    WORKERS=4 \
    LOG_LEVEL=info

# Run uvicorn directly
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]