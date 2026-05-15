# ── Base: Ubuntu 22.04 ────────────────────────────
FROM ubuntu:22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg from Ubuntu 22.04 apt is compiled with --enable-nvenc and --enable-cuda
# so NVENC works as long as the CUDA runtime + host drivers are present.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        curl \
        gnupg \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        python3.11-dev \
        python3-pip \
        ffmpeg \
        libass9 \
        fonts-dejavu-core \
        fonts-dejavu \
        nodejs \
        npm \
    && ln -sf /usr/bin/nodejs /usr/local/bin/node \
    && rm -rf /var/lib/apt/lists/*

# Make python3.11 / pip the default (FIXED ENSUREPIP ERROR HERE)
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11 \
    && python3.11 -m pip install --upgrade pip

# ── Python dependencies ────────────────────────────────────────────────────────
FROM base AS deps

WORKDIR /build
COPY requirements.txt .
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U yt-dlp

# ── Final image ────────────────────────────────────────────────────────────────
FROM base AS final

# Create non-root user
RUN groupadd -r clipper && useradd -r -g clipper -d /app -s /sbin/nologin clipper

WORKDIR /app

# Copy virtual environment from deps stage
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN chmod +x /usr/local/bin/deno

# Copy application code
COPY --chown=clipper:clipper . .

# Create cache directories and set permissions
RUN mkdir -p /app/.cache/yt-dlp /app/.cache/huggingface \
    && chown -R clipper:clipper /app/.cache

USER clipper

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run the API server.
# Override CMD in docker-compose / Kubernetes for the Celery worker:
#   command: ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=info"]
CMD ["python3.11", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]