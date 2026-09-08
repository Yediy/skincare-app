# syntax=docker/dockerfile:1

# Builder: compiles/resolves wheels for anything without a prebuilt
# manylinux wheel for this platform. Nothing in this stage ships in
# the final image -- only the installed Python packages are copied
# out of it.
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime: only what's actually needed to run the app. libgl1 and
# libglib2.0-0 are not optional here despite opencv-python-headless's
# name -- mediapipe's native backend still links against them even
# with no display attached (confirmed by this image's own build/run
# below, not assumed).
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .

RUN chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENABLE_DOCS=false

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
