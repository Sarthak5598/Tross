import asyncio
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import jobs
from automation import browser_pool, runner
from automation.runner import run_patient_job_api, CARE_PLAN_SECTIONS

app = FastAPI(title="Tross-trail")

# Browsers block cross-origin calls unless the server opts in, and without
# this a page served from anywhere other than this host gets an opaque
# "Failed to fetch" with no useful error. Swagger at /docs is same-origin
# so it worked regardless, which is exactly why this was easy to miss.
#
# Wide open, matching the current deployment: the endpoint has no auth and
# is deliberately reachable by anyone. Tighten `allow_origins` to the real
# caller list at the same time auth goes in — the two decisions belong
# together, since neither one alone restricts access.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    # Only launch Chromium when we might actually need it. With the HTTP
    # login there is nothing for a browser to do, and not starting one
    # saves several hundred MB on a 1GB instance. browser_pool starts
    # lazily if the fallback is ever used.
    import config
    if not config.USE_HTTP_LOGIN:
        await browser_pool.start()
    # Log in and capture API credentials at boot, then keep the token
    # renewed in the background, so no request ever waits ~30s for MFA.
    from automation.api_session import session
    asyncio.create_task(_warm_api_session(session))


async def _warm_api_session(session) -> None:
    try:
        await session.warm_up()
    except Exception:
        pass          # /health surfaces the failure; retried by the loop
    await session.run_renewal_loop()


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
    # Caller sent something we (or athenahealth) reject outright — an
    # unknown department code, a non-numeric patient id. Retrying it
    # unchanged will never work, so it must not look like a server fault.
    "invalid_request": 400,
    # Anything else: a genuine bug or unexpected page state on our side.
    "automation_error": 500,
}


@app.get("/api/departments")
async def list_departments():
    """The eight valid `department` codes, so callers can discover them
    rather than hardcoding display labels that may be renamed upstream."""
    from automation.departments import catalog
    return {"departments": catalog()}


@app.get("/health")
async def health():
    """Liveness probe. `apiSession` is the important part: it reports token
    freshness and the last acquisition error, so a browser session that has
    died is visible here rather than only as failing requests."""
    from automation.api_session import session
    return {"status": "ok", "apiSession": session.status()}


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


@app.post("/api/patient")
async def start_patient_job(
    patient_id: str,
    background_tasks: BackgroundTasks,
    sections: str | None = None,
    department: str | None = None,
    include_history: bool = False,
    include_care_plan: bool = False,
    include_archived: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
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

    `include_history`: goal progress history, **off by default**. It costs
    one API call per goal, cannot be batched, and is roughly a third of
    total request time (12.2s -> 8.5s without it on a 32-goal patient).
    Most callers don't need it, so they shouldn't pay for it. Everything
    else about the goals — objectives, interventions, baseline, modalities
    — is unaffected: those come free with the main care-plan call.

    `include_care_plan`: athenahealth stores a separate Care Plan
    (`HealthConcernType: Longitudinal`) alongside the Treatment Plan. This
    service is about the Treatment Plan, so it is excluded by default.

    `include_archived`: a patient can carry several Treatment Plans with
    the older ones archived — 1135 and 1136 each have three. Merging them
    reports superseded goals as current (14 goals where the live plan has
    5), so archived plans are excluded by default. Nothing is hidden
    silently: `planScope` in the response always reports what was left
    out, and every row is tagged `plan_type` / `is_archived`.

    `start_date` / `end_date`: optional ISO dates (`2026-09-01`) or full
    timestamps, defaulting to everything on record. Be aware they do not
    affect every section equally — measured on patient 1133:

      * goal progress history genuinely filters (a Jan-Jun 2026 window
        returns 0 of 2 status entries)
      * task schedules do NOT — athenahealth returned the same 57
        objectives/interventions for an 81-year range and for a single day

    So this narrows history, not the plan itself. The range is still sent
    to both calls, since it is a required argument and may start being
    honoured.

    `department`: optional. Accepts the code (`SH_OH_SHAKER`, case
    -insensitive), athenahealth's numeric id (`4`), or the display label
    (`SH OH - Shaker`) — see GET /api/departments for all three.
    Note it does not affect what a care-plan query returns — verified by
    issuing the same query under three departments and getting identical
    responses — so it is effectively informational. Display labels are
    also accepted for backwards compatibility.

    `wait`: if true, the request blocks until the run finishes and returns
    the data directly, with a real HTTP status — 200, 404 (patient not
    found), 503 (athenahealth unreachable), 504 (timed out), 500
    (automation failure). Default false returns `{jobId}` immediately for
    the poll-and-watch flow the Streamlit dashboard uses; in that mode
    failures surface on the job record, since the response is already sent
    before anything can go wrong.

    Idempotent within a short window: an identical request (same patient_id
    + sections + department + include_history) made again within
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

    cache_key = jobs.make_cache_key(patient_id, wanted, department, include_history,
                                    include_care_plan, include_archived,
                                    start_date, end_date)
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

    # No browser in the request path, so there is no single-job lock and no
    # 409-on-busy: requests are plain concurrent HTTP calls upstream.
    job_id = str(uuid.uuid4())
    jobs.cache_job(cache_key, job_id)

    if wait:
        # Run inline rather than as a BackgroundTask — those only execute
        # after the response is sent, which would defeat the point.
        await run_patient_job_api(job_id, patient_id, wanted, department,
                                  include_history, include_care_plan,
                                  include_archived, start_date, end_date)
        return _job_to_http(jobs.get(job_id), job_id)

    background_tasks.add_task(run_patient_job_api, job_id, patient_id,
                              wanted, department, include_history,
                              include_care_plan, include_archived,
                              start_date, end_date)
    return {"jobId": job_id, "deduped": False}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
