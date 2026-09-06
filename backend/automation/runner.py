import asyncio
from datetime import datetime, timezone

import jobs
# Browser-side errors: the browser is no longer in the request path, but
# startup warm-up still drives a real login + patient search, so these can
# still surface from there.
from automation.patient_search import (
    PatientNotFoundError,
    PatientRecordMismatchError,
)


# Hard ceiling on a single job, so a stuck upstream call can't pin a job
# open indefinitely. Generous relative to a real run (~12s with history,
# ~8.5s without) because it is a backstop, not a latency target.
JOB_TIMEOUT_S = 300

# An unreachable upstream shows up either as a urllib socket error here or
# as a net::ERR_* from Chromium during startup warm-up. Worth separating
# from a generic bug: if athenahealth is down, nothing is wrong with our
# code and the caller should see 503, not 500.
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
    from automation.graphql import PatientNotFound, InvalidRequest

    text = str(exc)
    # The API path raises its own not-found (athenahealth embeds CODE: 404
    # in a 200 body); map it to the same errorType the DOM path uses so a
    # caller sees one contract regardless of source.
    if isinstance(exc, PatientNotFound):
        return "patient_not_found", text
    if isinstance(exc, (InvalidRequest, ValueError)):
        return "invalid_request", text
    if isinstance(exc, PatientNotFoundError):
        return "patient_not_found", text
    if isinstance(exc, PatientRecordMismatchError):
        return "patient_record_mismatch", text
    if any(marker in text.lower() for marker in NETWORK_ERROR_MARKERS):
        return "site_unavailable", f"athenahealth appears to be unreachable: {text}"
    return "automation_error", text



# The sections a caller can ask for. A caller who only wants to confirm a
# patient exists passes an empty set and pays for one call; one who wants
# everything omits the param.
CARE_PLAN_SECTIONS = {"summary", "attestations", "concerns", "goals", "characteristics"}

# "Everything on record." Both date-taking operations declare their range
# as String! — required — so a range is always sent; these are what we
# send when the caller doesn't narrow it.
DEFAULT_START_DATE = "2018-01-01"
DEFAULT_END_DATE = "2099-12-31"


def _normalise_dates(start_date: str | None, end_date: str | None) -> tuple:
    """Turn caller dates into the two formats the two operations want.

    Task schedules take a plain date (`2026-09-01`); goal history takes a
    full ISO instant (`2026-09-01T00:00:00.000Z`). One input, both derived.

    Parsed into real datetimes and formatted back out, rather than
    manipulated as strings. The string approach passed validation and then
    emitted nonsense for perfectly legal inputs:

        2026-09-01T10:00:00+05:30  ->  ...+05:30Z   (two offsets)
        2026-09-01 10:00:00        ->  '2026-09-01 10:00:00T00:00:00.000Z'
        20260901                   ->  '20260901T00:00:00.000Z'

    Worth knowing what the range actually buys, because the two operations
    do NOT behave the same (measured on patient 1133):

      * GetGoalStatusHistoryInternal  — genuinely filters. Jan-Jun 2026
        returns 0 of the 2 status entries.
      * GetTaskSchedulesWithScheduledTasks — does NOT. Same 57 schedules
        for an 81-year range and for a single day.

    So narrowing trims progress history and leaves objectives and
    interventions untouched. The range is still sent to both, since it is
    a required argument and may start being honoured.
    """
    def parse(label: str, value: str) -> tuple[datetime, bool]:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"{label}={value!r} is not a valid ISO date. Use YYYY-MM-DD "
                f"or a full ISO timestamp such as 2026-09-01T10:00:00Z.") from None
        # No time given means the caller meant a whole day, which the two
        # ends of the range interpret differently — see below.
        date_only = "T" not in value and " " not in value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), date_only

    start, start_date_only = parse("start_date", (start_date or DEFAULT_START_DATE).strip())
    end, end_date_only = parse("end_date", (end_date or DEFAULT_END_DATE).strip())

    # A bare end date means the END of that day. Without this,
    # start=end=2026-09-06 would be a zero-width window and return nothing.
    if end_date_only:
        end = end.replace(hour=23, minute=59, second=59, microsecond=999000)

    if start > end:
        raise ValueError(
            f"start_date {start.isoformat()} is after end_date {end.isoformat()}")

    def instant(value: datetime) -> str:
        return (value.strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{value.microsecond // 1000:03d}Z")

    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            instant(start), instant(end))


async def _fetch_via_graphql(patient_id: str, wanted: set[str],
                             department: str | None, on_step,
                             include_history: bool = False,
                             include_care_plan: bool = False,
                             include_archived: bool = False,
                             start_date: str | None = None,
                             end_date: str | None = None) -> dict:
    """Read a patient's Treatment Plan from the Care Management API.

    The browser is not in this path at all — it only supplied the session
    headers. Everything here is HTTP, which is why the independent calls
    can run concurrently instead of contending over one page.
    """
    from automation.api_session import session
    from automation.extract_gql import build_result, select_concerns
    from automation.graphql import TokenExpired

    task_start, task_end, hist_start, hist_end = _normalise_dates(start_date, end_date)
    from automation.departments import resolve
    resolved = resolve(department)          # raises ValueError -> 400
    department_id = resolved.athena_id if resolved else None

    client = await session.client(department_id)
    await on_step("Acquired API credentials")

    async def run(operation, **variables):
        """Retry once on an expired token — it only lives ~5 minutes, so a
        request can outlive the token it started with."""
        try:
            return await client.run(operation, **variables)
        except TokenExpired:
            await session.invalidate()
            retry = await session.client(department_id)
            return await retry.run(operation, **variables)

    plan = await run("GetPatientCarePlanInternal", patientId=patient_id)
    all_concerns = (plan.get("getPatientCarePlanInternal") or {}).get("HealthConcerns") or []
    # Filter BEFORE the fan-out, not after. Archived plans can carry as
    # many goals as the live one (patients 1135/1136 have three plans
    # each), so filtering afterwards would mean firing — and paying for —
    # a progress-history call per archived goal only to discard it.
    concerns = select_concerns(all_concerns, include_care_plan, include_archived)
    skipped = len(all_concerns) - len(concerns)
    await on_step(f"Fetched care plan: {len(concerns)} health concern(s)"
                  + (f", {skipped} excluded (care plan / archived)" if skipped else ""))

    # Everything after the care plan depends only on the care plan — not
    # on each other. So it all goes out in ONE fan-out rather than in
    # phases: an earlier version awaited the supporting calls, then the
    # history calls, which serialised ~5.7s behind ~9.1s for no reason.
    jobs_: list[tuple[str, str, dict]] = []
    for concern in concerns:
        concern_id = str(concern.get("Id"))
        if "goals" in wanted:
            jobs_.append(("tasks:" + concern_id,
                          "GetTaskSchedulesWithScheduledTasks",
                          dict(patientId=patient_id, healthConcernId=concern_id,
                               startDate=task_start, endDate=task_end)))
            # Progress history is one call per goal and cannot be batched —
            # the bulk of the fan-out: one call per goal, un-batchable,
            # and roughly a third of total request time. Opt-in via
            # `include_history` rather than default-on.
            if include_history:
                for goal in (concern.get("Goals") or []):
                    jobs_.append((f"history:{goal.get('Id')}",
                                  "GetGoalStatusHistoryInternal",
                                  dict(patientId=patient_id,
                                       healthConcernId=concern_id,
                                       goalId=str(goal.get("Id")),
                                       startDate=hist_start,
                                       endDate=hist_end)))
        if "attestations" in wanted:
            jobs_.append(("attest:" + concern_id,
                          "GetAllHealthConcernAttestationsInternal",
                          dict(patientId=patient_id, healthConcernId=concern_id)))
    if wanted & {"characteristics", "concerns"}:
        jobs_.append(("observations", "GetObservations",
                      dict(patientId=patient_id)))

    results = await asyncio.gather(
        *(run(op, **variables) for _, op, variables in jobs_),
        return_exceptions=True)
    await on_step(f"Fetched {len(jobs_)} call(s) in one parallel fan-out")

    schedules, attestations, histories, observations = {}, {}, {}, None
    failures = 0
    for (key, _, _), value in zip(jobs_, results):
        if isinstance(value, Exception):
            failures += 1
            await on_step(f"Warning: {key} failed — {value}")
            continue
        if key.startswith("tasks:"):
            schedules[key.split(":", 1)[1]] = (
                (value.get("getTaskSchedulesWithScheduledTasks") or {})
                .get("TaskSchedules") or [])
        elif key.startswith("history:"):
            histories[key.split(":", 1)[1]] = value
        elif key.startswith("attest:"):
            attestations[key.split(":", 1)[1]] = value
        else:
            observations = value
    if failures:
        await on_step(f"{failures} call(s) failed; returning partial data")

    return build_result(plan, schedules, observations, attestations,
                        histories, wanted,
                        include_care_plan, include_archived)


async def run_patient_job_api(
    job_id: str,
    patient_id: str,
    sections: set[str] | None = None,
    department: str | None = None,
    include_history: bool = False,
    include_care_plan: bool = False,
    include_archived: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    """Run one patient extraction and record it against `job_id`.

    No browser in the request path — the browser only ever supplied the
    session headers — so nothing here contends over a shared page and
    requests run concurrently.
    """
    wanted = sections if sections is not None else CARE_PLAN_SECTIONS
    jobs.create(job_id)

    async def on_step(message: str) -> None:
        jobs.add_step(job_id, message)

    try:
        result = await asyncio.wait_for(
            _fetch_via_graphql(patient_id, wanted, department, on_step,
                               include_history, include_care_plan, include_archived,
                               start_date, end_date),
            timeout=JOB_TIMEOUT_S)
        jobs.set_department(job_id, department)
        jobs.finish(job_id, success=True, result=result)

    except asyncio.TimeoutError:
        message = f"Job exceeded the {JOB_TIMEOUT_S}s limit and was aborted."
        jobs.add_step(job_id, f"Error: {message}")
        jobs.finish(job_id, success=False, error=message, error_type="timeout")

    except Exception as exc:
        error_type, message = classify_error(exc)
        jobs.add_step(job_id, f"Error: {message}")
        jobs.finish(job_id, success=False, error=message, error_type=error_type)
