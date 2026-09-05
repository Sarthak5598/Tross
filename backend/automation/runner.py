import asyncio

import jobs
import config
from automation import frames, browser_pool
from automation.login import login
from automation.patient_search import (
    search_patient,
    PatientNotFoundError,
    PatientRecordMismatchError,
)
from automation.care_plan import (
    open_care_management_pane,
    extract_concerns,
    extract_behavioral_health_goals,
    extract_treatment_plan_summary,
    extract_attestation_artifacts,
    extract_client_characteristics,
)


# Hard ceiling on a single job. Without this, a hung job holds the
# single-job lock forever and every subsequent request gets a 409 until
# someone restarts the process — i.e. one hang is a full outage. Observed
# for real twice (20+ minute stalls), so this is not theoretical. Sized
# well above the slowest legitimate run seen (~85s cold, all sections).
JOB_TIMEOUT_S = 300

# Chromium surfaces an unreachable site as a net::ERR_* navigation failure.
# Worth separating from a generic automation bug: if athenahealth itself is
# down or unreachable, nothing is wrong with our code and the caller should
# see a 503 (upstream unavailable), not a 500.
NETWORK_ERROR_MARKERS = (
    "net::err",
    "err_connection",
    "err_name_not_resolved",
    "err_internet_disconnected",
    "err_address_unreachable",
    "err_connection_refused",
    "err_connection_timed_out",
    "err_tunnel_connection_failed",
)


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to (errorType, message) so callers can react
    without string-matching. See STATUS_FOR_ERROR_TYPE in main.py for how
    these become HTTP status codes."""
    text = str(exc)
    if isinstance(exc, PatientNotFoundError):
        return "patient_not_found", text
    if isinstance(exc, PatientRecordMismatchError):
        return "patient_record_mismatch", text
    if any(marker in text.lower() for marker in NETWORK_ERROR_MARKERS):
        return "site_unavailable", f"athenahealth appears to be unreachable: {text}"
    return "automation_error", text


async def _run_browser_job(
    job_id: str, work, department: str | None = None, live: bool = False
) -> None:
    """Shared lifecycle: reuse the persistent authenticated page (only
    logging in if the session isn't actually alive), switch department if
    the caller asked for one different from what's currently active, run
    `work(page, on_step)` (which may return a result to store on the job),
    optionally capture a screenshot after every step, record
    success/failure.

    `live`: whether to capture per-step screenshots for the live Streamlit
    view. Each screenshot is a real per-step cost (page.screenshot + JPEG
    encode) that a plain API caller who only wants the final JSON result
    never benefits from — default False skips it entirely. This only
    affects the frame-by-frame view; it does not change what gets
    extracted or returned.

    The page is intentionally never closed here — it's the shared session
    for the next job too. Caller is expected to have already reserved the
    single-job slot via jobs.try_start(job_id) — this always releases it
    when done.
    """
    jobs.create(job_id)
    frames.start_job(job_id)

    async def _drive() -> dict:
        page = await browser_pool.ensure_page()

        async def on_step(msg: str) -> None:
            jobs.add_step(job_id, msg)
            if not live:
                return
            try:
                frame = await page.screenshot(type="jpeg", quality=55)
                frames.add_frame(job_id, frame)
            except Exception:
                pass

        if await browser_pool.is_logged_in(page):
            await on_step("Reusing existing authenticated session")
        else:
            await on_step(f"Opening {config.ATHENA_LOGIN_URL}")
            await page.goto(config.ATHENA_LOGIN_URL)

            login_result = await login(
                page,
                username=config.ATHENA_USERNAME,
                password=config.ATHENA_PASSWORD,
                totp_secret=config.ATHENA_TOTP_SECRET,
                on_step=on_step,
            )
            browser_pool.record_login(login_result)

        await browser_pool.ensure_department(page, department, on_step)
        # Recorded on the job so a later "no Treatment Plan found" failure
        # is diagnosable — that's almost always a department-scoping issue.
        jobs.set_department(job_id, browser_pool.current_department())

        return await work(page, on_step)

    try:
        result = await asyncio.wait_for(_drive(), timeout=JOB_TIMEOUT_S)
        jobs.finish(job_id, success=True, result=result)

    except asyncio.TimeoutError:
        jobs.add_step(job_id, f"Error: job exceeded {JOB_TIMEOUT_S}s and was aborted")
        jobs.finish(
            job_id,
            success=False,
            error=(
                f"Job exceeded the {JOB_TIMEOUT_S}s limit and was aborted. The shared "
                f"browser session has been reset; retry the request."
            ),
            error_type="timeout",
        )
        # The shared page is likely wedged mid-navigation — drop it so the
        # next job starts from a clean one rather than inheriting the mess.
        await browser_pool.reset_page()

    except Exception as exc:
        error_type, message = classify_error(exc)
        jobs.add_step(job_id, f"Error: {message}")
        jobs.finish(job_id, success=False, error=message, error_type=error_type)

    finally:
        jobs.release(job_id)


async def run_login_job(job_id: str, live: bool = False) -> None:
    async def work(page, on_step):
        pass  # login itself is all we're testing here

    await _run_browser_job(job_id, work, live=live)


CARE_PLAN_SECTIONS = {"summary", "attestations", "concerns", "goals", "characteristics"}


async def run_patient_job(
    job_id: str,
    patient_id: str,
    sections: set[str] | None = None,
    department: str | None = None,
    shorter: bool = False,
    live: bool = False,
) -> None:
    """Single entry point for patient lookup + Treatment Plan data —
    replaces two previously separate jobs (a search-only one and a
    full-care-plan one) that each redid the login+search step
    independently. A caller who just wants to confirm a patient exists can
    pass `sections={}` (or a query with no sections) for the fastest
    possible response; a caller who wants everything just omits the params.

    `sections`, if given, is a subset of CARE_PLAN_SECTIONS — only those
    are extracted and returned. None means all sections (default,
    unchanged behavior).

    `shorter`, if True, skips expanding each Behavioral Health Goal's
    nested Objectives/Interventions/Baseline/Goal Progress History — the
    single slowest part of the flow — leaving just the goal's top-level
    fields (status, priority, title, dates, attribution). Default False
    means full detail (unchanged behavior).

    `department`, if given, switches the active department before
    searching — see automation.browser_pool.ensure_department.
    """
    wanted = sections if sections is not None else CARE_PLAN_SECTIONS

    async def work(page, on_step):
        await search_patient(page, patient_id, on_step)

        if not wanted:
            return {}

        await open_care_management_pane(page, on_step)

        frame = page.frame(name="frMain")
        result = {}

        # These four are all independent, read-only DOM parses off the
        # already-loaded pane — no clicks, no shared mutable UI state
        # between them (unlike goal expansion below, which must stay
        # sequential). Running them concurrently turns ~4 sequential round
        # trips into ~1, bounded by the slowest one.
        async def _summary_task():
            result["planSummary"] = await extract_treatment_plan_summary(frame)
            await on_step("Extracted treatment plan summary")

        async def _attestations_task():
            attestations = await extract_attestation_artifacts(frame)
            result["attestationArtifacts"] = attestations
            await on_step(f"Extracted {len(attestations)} attestation artifacts")

        async def _concerns_task():
            concerns = await extract_concerns(frame)
            result["concerns"] = concerns
            await on_step(f"Extracted {len(concerns)} concerns")

        async def _characteristics_task():
            result["clientCharacteristics"] = await extract_client_characteristics(frame)
            await on_step("Extracted client characteristics")

        parallel_tasks = []
        if "summary" in wanted:
            parallel_tasks.append(_summary_task())
        if "attestations" in wanted:
            parallel_tasks.append(_attestations_task())
        if "concerns" in wanted:
            parallel_tasks.append(_concerns_task())
        if "characteristics" in wanted:
            parallel_tasks.append(_characteristics_task())
        if parallel_tasks:
            await asyncio.gather(*parallel_tasks)

        if "goals" in wanted:
            goals = await extract_behavioral_health_goals(frame, on_step, expand=not shorter)
            result["behavioralHealthGoals"] = goals
            note = " (top-level fields only, not expanded)" if shorter else ""
            await on_step(f"Extracted {len(goals)} behavioral health goals{note}")

        return result

    await _run_browser_job(job_id, work, department=department, live=live)
