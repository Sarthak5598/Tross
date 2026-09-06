"""In-memory job tracking, one dict per job_id, plus the short
idempotency window. No DB yet.

Jobs run concurrently: there is no single-job lock any more. That lock
existed only because the old DOM path shared one browser page — with all
data coming from HTTP calls, nothing contends.
"""

import time
from datetime import datetime, timezone

_jobs: dict[str, dict] = {}
_start_times: dict[str, float] = {}

# Short-window idempotency cache: if the exact same request (same key)
# comes in again within IDEMPOTENCY_TTL_SECONDS — whether the first call is
# still running or just finished — hand back the same job instead of
# starting work again. This is NOT the long-lived "freshness" cache/DB
# storage discussed separately (stakeholder was explicit that must still
# hit Athena live every real request); it only protects against
# duplicate/retried calls for the same thing in a short window.
IDEMPOTENCY_TTL_SECONDS = 60
_idempotency_cache: dict[tuple, dict] = {}


def make_cache_key(patient_id: str, sections, department,
                   include_history: bool = False,
                   include_care_plan: bool = False,
                   include_archived: bool = False,
                   start_date: str | None = None,
                   end_date: str | None = None) -> tuple:
    # `include_history` is part of the key: a caller asking for history
    # must never be deduped onto a cached run that skipped it and handed
    # back a goal list with every progress history empty.
    sections_key = tuple(sorted(sections)) if sections is not None else None
    return (patient_id, sections_key, department, include_history,
            include_care_plan, include_archived, start_date, end_date)


def get_cached_job_id(key: tuple) -> str | None:
    entry = _idempotency_cache.get(key)
    if entry is None:
        return None
    if time.monotonic() > entry["expires_at"]:
        del _idempotency_cache[key]
        return None
    return entry["job_id"]


def cache_job(key: tuple, job_id: str) -> None:
    _idempotency_cache[key] = {"job_id": job_id, "expires_at": time.monotonic() + IDEMPOTENCY_TTL_SECONDS}


def invalidate_cache(key: tuple) -> None:
    _idempotency_cache.pop(key, None)


# Jobs accumulate for the life of the process, so cap how many we keep —
# an always-on server would otherwise grow without bound. Oldest first out
# (dicts preserve insertion order).
MAX_RETAINED_JOBS = 100


def create(job_id: str) -> None:
    _jobs[job_id] = {
        "status": "running",
        "steps": [],
        "error": None,
        # Machine-readable failure class, so a caller can tell "you gave me
        # a patient ID that doesn't resolve" (don't retry, fix the input)
        # apart from "the automation broke" (retryable). None on success.
        "errorType": None,
        "result": None,
        # Which athenahealth department this ran against. Recorded because
        # "no Treatment Plan found" is usually a department-scoping issue,
        # and without this the failure is undiagnosable after the fact.
        "department": None,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "finishedAt": None,
    }
    _start_times[job_id] = time.monotonic()

    while len(_jobs) > MAX_RETAINED_JOBS:
        oldest = next(iter(_jobs))
        _jobs.pop(oldest, None)
        _start_times.pop(oldest, None)


def set_department(job_id: str, department: str | None) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["department"] = department


def add_step(job_id: str, message: str) -> None:
    """Steps are readable via GET /api/jobs/{id}, so they get the same
    treatment as error messages."""
    from automation.redact import redact
    message = redact(message)
    # Defensive: a job can be evicted (see MAX_RETAINED_JOBS) while a very
    # long-running one is still emitting steps. Losing a log line is fine;
    # crashing the automation over it is not.
    job = _jobs.get(job_id)
    if job is None:
        return
    elapsed = time.monotonic() - _start_times.get(job_id, time.monotonic())
    job["steps"].append(f"[{elapsed:5.2f}s] {message}")


def finish(
    job_id: str,
    success: bool,
    error: str | None = None,
    result=None,
    error_type: str | None = None,
) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    job["status"] = "done" if success else "failed"
    job["error"] = error
    job["errorType"] = error_type
    job["result"] = result
    job["finishedAt"] = datetime.now(timezone.utc).isoformat()


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)
