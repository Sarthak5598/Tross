import asyncio
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import Response

import jobs
from automation import frames, browser_pool, runner
from automation.runner import (
    run_login_job,
    run_patient_job,
    CARE_PLAN_SECTIONS,
)

app = FastAPI(title="Tross-trail")


@app.on_event("startup")
async def _startup() -> None:
    # Keep one Chromium instance warm across requests — cold-launching one
    # per job was costing ~12-15s every single call.
    await browser_pool.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await browser_pool.stop()


# How each failure class surfaces to an HTTP caller. Kept as an explicit
# map (rather than scattered raises) so every failure has a defined code
# and nothing falls through to a bare 500 by accident.
STATUS_FOR_ERROR_TYPE = {
    # The ID doesn't resolve to a record this account can see. Caller's
    # input problem — retrying unchanged won't help.
    "patient_not_found": 404,
    # Person exists, but under another provider group's record. Not a
    # retry — the caller needs to pick the right id or department.
    "patient_record_mismatch": 409,
    # athenahealth itself is unreachable. Nothing wrong on our side; this
    # is an upstream outage, so 503 rather than 500.
    "site_unavailable": 503,
    # Our own ceiling tripped (see runner.JOB_TIMEOUT_S) — the upstream
    # took too long, which is what 504 means.
    "timeout": 504,
    # Anything else: a genuine bug or unexpected page state on our side.
    "automation_error": 500,
}


@app.get("/health")
async def health():
    """Liveness probe for whatever ends up hosting this. Reports whether a
    job is currently occupying the single execution slot, so a hung
    instance is visible without reading logs."""
    return {"status": "ok", "activeJobId": jobs.active_job_id()}


async def _await_job(job_id: str) -> dict:
    """Block until a job reaches a terminal state. Used by `wait=true`.

    Bounded slightly above runner.JOB_TIMEOUT_S: the job always fails
    itself at that ceiling, so this only needs to outlive it rather than
    enforce its own limit.
    """
    deadline = asyncio.get_event_loop().time() + runner.JOB_TIMEOUT_S + 30
    while True:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] in ("done", "failed"):
            return job
        if asyncio.get_event_loop().time() >= deadline:
            raise HTTPException(
                status_code=504,
                detail={"message": "Timed out waiting for the job to finish", "jobId": job_id},
            )
        await asyncio.sleep(0.25)


def _job_to_http(job: dict, job_id: str) -> dict:
    """Return the result, or raise the HTTPException matching the failure."""
    if job["status"] == "done":
        return {"jobId": job_id, "department": job["department"], "result": job["result"]}
    raise HTTPException(
        status_code=STATUS_FOR_ERROR_TYPE.get(job["errorType"], 500),
        detail={
            "message": job["error"],
            "errorType": job["errorType"],
            "jobId": job_id,
            "department": job["department"],
        },
    )


def _reserve_job_slot() -> str:
    """Only one browser automation job runs at a time (shared browser +
    sandbox login throttling — see TROUBLESHOOTING.md #8). Raises 409 with
    the currently running job's id if one is already in flight."""
    job_id = str(uuid.uuid4())
    if not jobs.try_start(job_id):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Another job is already running",
                "activeJobId": jobs.active_job_id(),
            },
        )
    return job_id


@app.post("/api/login-test")
async def start_login_test(background_tasks: BackgroundTasks, live: bool = False):
    job_id = _reserve_job_slot()
    background_tasks.add_task(run_login_job, job_id, live)
    return {"jobId": job_id}


@app.post("/api/patient")
async def start_patient_job(
    patient_id: str,
    background_tasks: BackgroundTasks,
    sections: str | None = None,
    department: str | None = None,
    shorter: bool = False,
    live: bool = False,
    wait: bool = False,
):
    """Single endpoint for patient lookup + Treatment Plan data — replaces
    the old separate /api/patient-search and /api/care-plan, which each
    redid the login+search step independently for what was really one flow.

    `sections`: optional comma-separated subset of
    {summary, attestations, concerns, goals, characteristics}. Omit for
    everything (default). Pass an empty string for just a patient-found
    confirmation with no Treatment Plan data at all (the old
    /api/patient-search behavior).

    `shorter`: if true, Behavioral Health Goals are returned with their
    top-level fields only — Objectives/Interventions/Baseline/Goal
    Progress History (the slowest part of the flow) are skipped. Default
    false returns full detail, unchanged from before.

    `live`: if true, captures a screenshot after every step for the
    Streamlit live view. Default false skips this — pure per-step overhead
    for a caller who only wants the final JSON.

    `department`: optional exact department label to switch to first.

    `wait`: if true, the request blocks until the run finishes and returns
    the data directly, with a real HTTP status — 200, 404 (patient not
    found), 503 (athenahealth unreachable), 504 (timed out), 500
    (automation failure). Default false returns `{jobId}` immediately for
    the poll-and-watch flow the Streamlit dashboard uses; in that mode
    failures surface on the job record, since the response is already sent
    before anything can go wrong.

    Idempotent within a short window: an identical request (same patient_id
    + sections + department + shorter) made again within
    jobs.IDEMPOTENCY_TTL_SECONDS returns the same jobId instead of starting
    automation again — whether the first call is still running or just
    completed. This is a short dedup window against duplicate/retried
    calls, not a substitute for the separate "freshness" DB caching
    decision — a request outside the window always hits Athena live.
    """
    wanted = None
    if sections is not None:
        wanted = {s.strip() for s in sections.split(",") if s.strip()}
        unknown = wanted - CARE_PLAN_SECTIONS
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown section(s): {sorted(unknown)}. Valid: {sorted(CARE_PLAN_SECTIONS)}",
            )

    cache_key = jobs.make_cache_key(patient_id, wanted, department, shorter, live)
    cached_job_id = jobs.get_cached_job_id(cache_key)
    if cached_job_id:
        cached_job = jobs.get(cached_job_id)
        if cached_job and cached_job["status"] != "failed":
            if wait:
                # Deduped onto an in-flight (or just-finished) identical
                # run — wait on that one rather than starting a second.
                return _job_to_http(await _await_job(cached_job_id), cached_job_id)
            return {"jobId": cached_job_id, "deduped": True}
        jobs.invalidate_cache(cache_key)

    job_id = _reserve_job_slot()
    jobs.cache_job(cache_key, job_id)

    if wait:
        # Run inline rather than as a BackgroundTask — those only execute
        # after the response is sent, which would defeat the point.
        await run_patient_job(job_id, patient_id, wanted, department, shorter, live)
        return _job_to_http(jobs.get(job_id), job_id)

    background_tasks.add_task(run_patient_job, job_id, patient_id, wanted, department, shorter, live)
    return {"jobId": job_id, "deduped": False}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}/frames")
async def get_frame_count(job_id: str):
    return {"count": frames.count(job_id)}


@app.get("/api/jobs/{job_id}/frames/{index}")
async def get_frame(job_id: str, index: int):
    frame = frames.get_frame(job_id, index)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not found")
    return Response(content=frame, media_type="image/jpeg")
