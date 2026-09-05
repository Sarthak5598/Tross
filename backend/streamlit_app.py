import os
import time
from datetime import datetime

import requests
import streamlit as st

# Defaults to the deployed EC2 instance. Override for local work:
#   TROSS_API_BASE=http://127.0.0.1:8010 streamlit run streamlit_app.py
# Note the deployed IP is auto-assigned and changes if the instance is
# stopped and started — attach an Elastic IP to make it stable.
DEFAULT_API_BASE = "http://52.91.250.2:8000"
API_BASE = os.environ.get("TROSS_API_BASE", DEFAULT_API_BASE).rstrip("/")

st.set_page_config(page_title="Tross-trail — Live Test", page_icon="🔑", layout="wide")
st.markdown("### 🔑 Tross-trail — Live Test")
st.caption("Runs the athenahealth sandbox flow (login + TOTP, patient search, care plan) and streams the browser live.")


def _backend_status() -> tuple[str, str]:
    """Ping /health so it's obvious at a glance which backend this is
    talking to and whether it's up — otherwise a wrong/missing API_BASE
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
_icon = {"ok": "🟢", "busy": "🟡", "error": "🔴"}[_state]
st.caption(f"{_icon} Backend: `{API_BASE}` — {_detail}")
if _state == "error":
    st.error(
        f"Can't reach the backend at {API_BASE}. Check the instance is running and that "
        f"its public IP hasn't changed (it's reassigned on stop/start), or set "
        f"TROSS_API_BASE to a different host."
    )

MODES = ["Login only", "Patient lookup"]
mode = st.radio("Mode", MODES, horizontal=True)
live_view = st.checkbox(
    "Live view (screenshots)",
    value=True,
    help="Off = no per-step screenshots captured at all — use this to time the API without that overhead.",
)
patient_id = None
sections_param = None
department = None
shorter = False
if mode != "Login only":
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
    shorter = st.checkbox("Shorter (skip nested goal detail: Objectives/Interventions/Baseline/Progress)")
    department = st.text_input("Department (optional, exact label)", value="")

run = st.button("Run Test", type="primary")

live_col, steps_col = st.columns([1, 1])
with live_col:
    st.caption("Live Browser View")
    frame_box = st.empty()
    frame_box.markdown(
        "<div style='background:#0f1720;border:1px solid #223140;border-radius:8px;"
        "height:280px;display:flex;align-items:center;justify-content:center;color:#5b6b78;font-size:12px;'>"
        "No automation running</div>",
        unsafe_allow_html=True,
    )
with steps_col:
    st.caption("Step Log")
    steps_box = st.empty()

result_box = st.empty()
data_box = st.empty()


def poll_job(job_id: str) -> None:
    # A wall-clock deadline, not an iteration count — the full care-plan
    # flow has legitimately taken anywhere from ~40s to several minutes
    # across real runs (department-step and pane-load timing both vary a
    # lot), so a fixed iteration count either times out too early or wastes
    # time on faster runs. The backend job itself runs independently as a
    # FastAPI BackgroundTask — if this UI-side poll gives up, the job may
    # still complete; that's why we show the job_id on timeout below.
    deadline = time.monotonic() + 360  # 6 minutes
    seen = 0
    last_frame = -1

    while time.monotonic() < deadline:
        job = requests.get(f"{API_BASE}/api/jobs/{job_id}", timeout=5).json()
        steps = job.get("steps") or []
        if len(steps) > seen:
            steps_box.markdown("\n".join(f"- {s}" for s in steps))
            seen = len(steps)

        frames_info = requests.get(f"{API_BASE}/api/jobs/{job_id}/frames", timeout=5).json()
        frame_count = frames_info["count"]
        if frame_count - 1 > last_frame:
            last_frame = frame_count - 1
            frame_img = requests.get(
                f"{API_BASE}/api/jobs/{job_id}/frames/{last_frame}", timeout=5
            ).content
            frame_box.image(frame_img, caption=f"Live browser — step {last_frame + 1}", use_column_width=True)
            time.sleep(0.4)

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
    if mode == "Login only":
        submit = requests.post(f"{API_BASE}/api/login-test", params={"live": live_view}, timeout=10)
    else:
        params = {"patient_id": patient_id, "shorter": shorter, "live": live_view}
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
