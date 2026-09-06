"""End-to-end check. Every wait is bounded — the previous bash harness
polled a job id that was never created and spun forever."""
import json, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

# Defaults to the deployed instance; override for local:
#   python tests/test_e2e.py http://localhost:8000
BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TROSS_API_BASE", "http://52.91.250.2:8000"))

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

print("\n-- plan filtering (1135 carries 3 plans, 2 archived) --")
for q, label in (("patient_id=1135", "default"),
                 ("patient_id=1135&include_archived=true", "+archived"),
                 ("patient_id=1133&include_care_plan=true", "1133 +care plan")):
    _, job, _ = run(q + "&wait=true")
    r = job.get("result") or {}
    sc = r.get("planScope") or {}
    print(f"  {label:<16} goals={len(r.get('behavioralHealthGoals') or []):<3} "
          f"plans={sc.get('returned')}/{sc.get('totalOnRecord')} "
          f"archivedExcluded={sc.get('excludedArchived')}")

print("\n-- date filter (must narrow history only) --")
for q, label in (("include_history=true", "all dates"),
                 ("include_history=true&start_date=2026-01-01&end_date=2026-06-30", "jan-jun 2026")):
    _, job, _ = run(f"patient_id=1133&wait=true&{q}")
    g = (job.get("result") or {}).get("behavioralHealthGoals") or []
    print(f"  {label:<14} goalsWithHistory={sum(1 for x in g if x['goal_progress_history']):<3} "
          f"objectives={sum(len(x.get('objectives') or []) for x in g)}")

print("\n-- idempotency --")
a = call("POST", "/api/patient?patient_id=1137")[1]
b = call("POST", "/api/patient?patient_id=1137")[1]
c = call("POST", "/api/patient?patient_id=1137&include_history=true")[1]
print(f"  repeat -> same job: {a.get('jobId') == b.get('jobId')}   deduped={b.get('deduped')}")
print(f"  differing flag -> new job: {c.get('jobId') != a.get('jobId')}")

print("\n-- departments --")
code, body = call("GET", "/api/departments")
print(f"  GET /api/departments -> {code}, {len(body.get('departments') or [])} listed")
for d, label in (("SH_OH_SHAKER", "code"), ("4", "numeric id"), ("SH%20OH%20-%20Shaker", "label")):
    c2, _ = call("POST", f"/api/patient?patient_id=1133&wait=true&sections=summary&department={d}")
    print(f"  {label:<12} -> {c2}")

print("\n-- token health --")
_, h = call("GET", "/health")
sess = h.get("apiSession", {})
print(f"  hasToken={sess.get('hasToken')} remaining={sess.get('secondsRemaining')}s "
      f"lastError={sess.get('lastError')}")

print("\n-- no secrets in an error body --")
_, err = call("POST", "/api/patient?patient_id=abc&wait=true")
blob = json.dumps(err)
leaky = re.findall(r'value="[^"]{4,}"', blob)
print(f"  body {len(blob)} chars, unredacted value= attributes: {len(leaky)}")

print("\n-- 4 concurrent (no single-job lock any more) --")
t0 = time.time()
with ThreadPoolExecutor(4) as pool:
    out = list(pool.map(lambda i: call("POST", f"/api/patient?patient_id=113{i}&wait=true", 180),
                        (3, 4, 5, 6)))
print(f"  statuses={[c for c, _ in out]}  wall={time.time()-t0:.1f}s")
