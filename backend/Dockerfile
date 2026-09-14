# syntax=docker/dockerfile:1

# Base image pinned by digest (not just the `3.11-slim` tag, which is
# mutable and moves under it) so a build today and a build next month
# use the same bytes. Chosen empirically -- see
# CI_SECURITY_RECONCILIATION.md's container-findings section: the
# `-bookworm` (Debian 12) variant was measured with more HIGH/CRITICAL
# OS findings than current `3.11-slim` (Debian 13/trixie), so it is
# not a safer choice by default. Refresh policy: re-pull `python:
# 3.11-slim`, re-run this Dockerfile's build/run/Trivy checks, and
# update the digest below on a regular cadence (recommended: monthly,
# or immediately if Trivy's full-report scan surfaces a new CRITICAL
# in the current digest) -- a pinned digest that's never refreshed
# just fossilizes today's vulnerabilities instead of floating them.

# Builder: compiles/resolves wheels for anything without a prebuilt
# manylinux wheel for this platform. Nothing in this stage ships in
# the final image -- only the installed Python packages are copied
# out of it.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder

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
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# `apt-get upgrade` here is a deliberate, honest tradeoff, NOT a
# reproducible build -- correcting an earlier version of this comment
# that claimed both at once, which isn't possible:
#
#   - The digest pin above fixes the base image's own bytes: a build
#     today and a build next month start `FROM` the exact same layers.
#   - This `apt-get upgrade` step then resolves whatever OS package
#     versions the live Debian package mirror currently serves --
#     that mirror's contents change over time (new security patches
#     land on it continuously), so THIS step is not pinned to
#     anything, and two builds of this exact Dockerfile on two
#     different days can end up with different OS package versions
#     baked into the final image.
#   - So: base image bytes are pinned; OS package patch level is
#     resolved from the live Debian repository at build time; the
#     final image is therefore NOT bit-reproducible across builds.
#     That's an intentional choice, not an oversight -- CI prefers
#     shipping current security fixes (this pinned digest's
#     perl-base/perl-Archive-Tar/sqlite3 packages carry CVE-2026-13221
#     and others that Trivy's gating scan correctly flags as
#     CRITICAL/HIGH with a fix already available) over waiting for
#     upstream to republish `python:3.11-slim` under a new digest that
#     would let a fully pinned build carry the same patches (this pass
#     checked: as of this build, `python:3.11-slim` still resolves to
#     the exact digest already pinned here -- no newer base image
#     exists yet to bump to).
#
# A future architecture that gets both properties -- reproducibility
# AND current patches, without this live-mirror variability -- would
# have CI build once, security-scan that one build, and push its
# resulting image digest immutably to a registry; every deployment
# then promotes that already-scanned digest rather than rebuilding.
# That registry/promotion pipeline does not exist yet and is not part
# of this change -- documented here as the direction, not built now.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY --from=builder /install /usr/local

# The app never installs packages at runtime -- pip/setuptools/wheel
# (and setuptools' vendored copies, e.g. jaraco.context) are build-time
# tools only, and their vendored dependencies are a real source of
# Trivy findings (e.g. CVE-2026-24049 in the wheel version pip itself
# bundles) for something this image never uses after the build stage.
RUN find /usr/local/lib/python3.11/site-packages -maxdepth 1 \
        \( -iname 'pip' -o -iname 'pip-*' -o -iname 'setuptools' -o -iname 'setuptools-*' \
           -o -iname 'wheel' -o -iname 'wheel-*' -o -iname 'pkg_resources' \
           -o -iname '_distutils_hack' -o -iname 'distutils-precedence.pth' \) \
        -exec rm -rf {} + \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 /usr/local/bin/wheel

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
