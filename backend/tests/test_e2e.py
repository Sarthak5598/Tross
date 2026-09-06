"""End-to-end check. Every wait is bounded — the previous bash harness
polled a job id that was never created and spun forever."""
import json, subprocess, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:8042"

def call(method, path, timeout=90):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"err": str(e)}

def wait_health(limit=240):
    end = time.time() + limit
    while time.time() < end:
        code, body = call("GET", "/health", timeout=10)
        if code == 200 and body["apiSession"].get("hasToken"):
            return True, body
        time.sleep(5)
    return False, body                      # bounded: gives up and reports

def run(path, limit=120):
    code, body = call("POST", "/api/patient?" + path)
    if code != 200:
        return code, body, None
    job_id = body["jobId"]
    end = time.time() + limit
    while time.time() < end:                # bounded
        _, job = call("GET", f"/api/jobs/{job_id}")
        if job.get("status") in ("done", "failed"):
            return code, job, job_id
        time.sleep(1)
    return code, {"status": "POLL TIMEOUT"}, job_id

ok, health = wait_health()
print("warm-up:", "OK" if ok else "FAILED", json.dumps(health.get("apiSession", {}))[:200])
if not ok:
    sys.exit(1)

print("\n-- happy paths --")
for q in ("patient_id=1133", "patient_id=1133&include_history=true",
          "patient_id=1133&sections=summary",
          "patient_id=1133&department=SH_OH_SHAKER", "patient_id=1134"):
    _, job, _ = run(q)
    r = job.get("result") or {}
    g = r.get("behavioralHealthGoals") or []
    took = job.get("steps", ["?"])[-1].split("]")[0].strip("[ ") if job.get("steps") else "?"
    print(f"  {q:<42} {job.get('status'):<6} {took:>7}  goals={len(g)} "
          f"hist={sum(1 for x in g if x.get('goal_progress_history'))} "
          f"concerns={len(r.get('concerns') or [])} "
          f"planSummary={len(r.get('planSummary') or [])} "
          f"chars={len(r.get('clientCharacteristics') or {})} "
          f"attest={len(r.get('attestationArtifacts') or [])}")

print("\n-- error contract --")
for q, want in (("patient_id=999999", 404), ("patient_id=1131", 404),
                ("patient_id=1132", 404), ("patient_id=abc", 400),
                ("patient_id=1133&department=Nope", 400)):
    code, body = call("POST", f"/api/patient?{q}&wait=true&sections=summary")
    d = body.get("detail", {})
    mark = "OK " if code == want else "BAD"
    print(f"  {mark} {q:<38} {code} (want {want})  {str(d.get('message'))[:50]}")

print("\n-- 4 concurrent (no single-job lock any more) --")
t0 = time.time()
with ThreadPoolExecutor(4) as pool:
    out = list(pool.map(lambda i: call("POST", f"/api/patient?patient_id=113{i}&wait=true", 180),
                        (3, 4, 5, 6)))
print(f"  statuses={[c for c, _ in out]}  wall={time.time()-t0:.1f}s")
