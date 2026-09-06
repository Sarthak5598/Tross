import os
import time
from datetime import datetime

import requests
import streamlit as st

# Local by default. The backend is editable in the UI below rather than
# hardcoded, because which one you want changes constantly — local while
# developing, the deployed instance while demoing — and the deployed IP is
# auto-assigned, so it moves whenever the instance is stopped and started.
LOCAL_API_BASE = "http://127.0.0.1:8010"
DEPLOYED_API_BASE = "http://52.91.250.2:8000"

st.set_page_config(page_title="Tross-trail — Live Test", page_icon="🔑", layout="wide")
st.markdown("### 🔑 Tross-trail — Live Test")
st.caption("Reads Treatment Plan data from athenahealth's Care Management API. "
           "A browser logs in once at startup to supply the session token; "
           "requests themselves are plain HTTP and run concurrently.")

_base_col, _status_col = st.columns([3, 2])
with _base_col:
    API_BASE = st.text_input(
        "Backend",
        value=os.environ.get("TROSS_API_BASE", LOCAL_API_BASE),
        help=f"Local: {LOCAL_API_BASE} · Deployed: {DEPLOYED_API_BASE}",
    ).rstrip("/")


def _backend_status() -> tuple[str, str]:
    """Ping /health so it's obvious at a glance which backend this is
    talking to and whether it's up — otherwise pointing at the wrong one
    only shows up as a confusing failure after you click Run."""
    try:
        r = requests.get(f"{API_BASE}/health", timeout=4)
        if r.status_code != 200:
            return "error", f"HTTP {r.status_code}"
        active = r.json().get("activeJobId")
        return ("busy", f"job {active[:8]}… running") if active else ("ok", "idle")
    except Exception as exc:
        return "error", type(exc).__name__


_state, _detail = _backend_status()
with _status_col:
    _icon = {"ok": "🟢", "busy": "🟡", "error": "🔴"}[_state]
    st.markdown(f"<div style='padding-top:2rem'>{_icon} {_detail}</div>", unsafe_allow_html=True)

if _state == "error":
    st.error(
        f"Can't reach a backend at {API_BASE}. If you meant to run locally, start it with "
        f"`uvicorn main:app --port 8010`. If you meant the deployed one, check the instance "
        f"is running and that its public IP hasn't changed — it's reassigned on stop/start."
    )

sections_param = None
department = None
include_history = False
patient_id = st.text_input("Patient ID", value="1133")
sections_mode = st.radio(
    "Sections", ["All (default)", "Patient-found confirmation only", "Custom"], horizontal=True
)
if sections_mode == "All (default)":
    sections_param = None
elif sections_mode == "Patient-found confirmation only":
    sections_param = ""
else:
    chosen = st.multiselect(
        "Pick sections", ["summary", "attestations", "concerns", "goals", "characteristics"]
    )
    sections_param = ",".join(chosen)
include_history = st.checkbox(
    "Include goal progress history (slower: one extra call per goal)")
# A dropdown rather than free text, defaulting to a real department so
# runs stay reproducible. Shows the human label but sends the stable
# code — a label rename upstream then costs one line here instead of
# breaking every saved request.
from automation.departments import Department
_choice = st.selectbox("Department", list(Department),
                       format_func=lambda d: f"{d.label}  ({d.value})",
                       index=1)
department = _choice.value

run = st.button("Run Test", type="primary")

st.caption("Step Log")
steps_box = st.empty()

result_box = st.empty()
data_box = st.empty()


def poll_job(job_id: str) -> None:
    # A wall-clock deadline rather than an iteration count. Runs are now
    # ~8.5s (or ~12s with history), but this stays generous: the backend
    # job runs independently as a FastAPI BackgroundTask, so if this
    # UI-side poll gives up the job may still finish — which is why the
    # job_id is shown on timeout below.
    deadline = time.monotonic() + 120
    seen = 0

    while time.monotonic() < deadline:
        job = requests.get(f"{API_BASE}/api/jobs/{job_id}", timeout=5).json()
        steps = job.get("steps") or []
        if len(steps) > seen:
            steps_box.markdown("\n".join(f"- {s}" for s in steps))
            seen = len(steps)

        if job["status"] in ("done", "failed"):
            elapsed_str = ""
            if job.get("startedAt") and job.get("finishedAt"):
                started = datetime.fromisoformat(job["startedAt"])
                finished = datetime.fromisoformat(job["finishedAt"])
                elapsed_str = f" ({(finished - started).total_seconds():.1f}s total)"

            if job["status"] == "done":
                result_box.success(f"Completed successfully.{elapsed_str}")
                if job.get("result"):
                    data_box.json(job["result"])
            else:
                result_box.error(f"Failed: {job.get('error')}{elapsed_str}")
            return
        time.sleep(0.35)

    result_box.warning(
        f"Gave up watching after 6 minutes, but the job may still be running in the "
        f"background — job ID: `{job_id}`. Paste it below and click 'Check job' to see "
        f"its current status without starting a new run."
    )


st.divider()
st.caption("Re-check a job that's still running in the background (e.g. after a timeout above)")
check_col1, check_col2 = st.columns([3, 1])
with check_col1:
    existing_job_id = st.text_input("Job ID", label_visibility="collapsed", placeholder="Paste a job ID here")
with check_col2:
    check = st.button("Check job")


if run:
    params = {"patient_id": patient_id, "include_history": include_history}
    if sections_param is not None:
        params["sections"] = sections_param
    if department:
        params["department"] = department
    submit = requests.post(f"{API_BASE}/api/patient", params=params, timeout=10)

    if submit.status_code != 200:
        st.error(f"Error starting job: {submit.status_code} {submit.text}")
    else:
        poll_job(submit.json()["jobId"])

if check and existing_job_id:
    poll_job(existing_job_id.strip())
