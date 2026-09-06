# Chromium is installed via Playwright's own installer rather than a
# pinned mcr.microsoft.com/playwright image tag, so the browser always
# matches whatever playwright version requirements-api.txt specifies —
# a mismatch between the two was the very first bug this project hit
# (TROUBLESHOOTING.md #1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Container runs as root, so Chromium's sandbox must be off — see
    # browser_pool._launch_args().
    CHROMIUM_NO_SANDBOX=1

WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt \
    && playwright install --with-deps chromium

COPY config.py jobs.py main.py ./
COPY automation ./automation

EXPOSE 8000

# Exactly ONE worker, always. The logged-in athena session, its token,
# the job records and the idempotency cache all live in process memory.
# A second worker would run its own login — and because TOTP codes are
# single-use within their 30-second window, two logins racing there means
# one of them simply fails. It would also dedupe against its own cache
# only, so identical concurrent requests would both do the work.
#
# This is not a throughput limit: requests are plain concurrent HTTP
# calls upstream and do not contend inside the process. Do not add
# --workers.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
