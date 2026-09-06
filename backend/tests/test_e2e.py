"""End-to-end check. Every wait is bounded — the previous bash harness
polled a job id that was never created and spun forever."""
import json, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

# Defaults to the deployed instance; override for local:
#   python tests/test_e2e.py http://localhost:8000
BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("TROSS_API_BASE", "http://52.91.250.2:8000"))

def call(method, path, timeout=90):
    """Returns (status, body). Status 0 means the request itself failed.

    The body is parsed as JSON when it is JSON and returned as {"text": ...}
    when it is not. That distinction matters: an earlier version parsed
    every response as JSON, so /docs and /redoc — which serve HTML — raised
    and were reported as status 0. The suite then failed and rolled back a
    perfectly healthy deploy. A test that reports a working endpoint as
    broken is worse than not testing it.
    """
    req = urllib.request.Request(BASE + path, method=method)

    def decode(status, raw):
        if not raw:
            return status, {}
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"text": raw[:200].decode("utf-8", "replace")}

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return decode(r.status, r.read())
    except urllib.error.HTTPError as e:
        return decode(e.code, e.read())
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

fails = []

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

print("\n-- every route responds --")
for method, path, want in (("GET", "/health", 200), ("GET", "/api/departments", 200),
                           ("GET", "/docs", 200), ("GET", "/redoc", 200),
                           ("GET", "/openapi.json", 200),
                           ("GET", "/api/jobs/does-not-exist", 404)):
    got, _ = call(method, path, timeout=20)
    if got != want:
        fails.append(f"{method} {path} -> {got}, wanted {want}")
    print(f"  {'OK ' if got == want else 'BAD'} {method:<5} {path:<26} {got}")

print("\n-- every sections value returns only what was asked for --")
SECTIONS = {"summary": "planSummary", "goals": "behavioralHealthGoals",
            "concerns": "concerns", "characteristics": "clientCharacteristics",
            "attestations": "attestationArtifacts"}
for name, key in SECTIONS.items():
    _, job, _ = run(f"patient_id=1133&wait=true&sections={name}")
    r = job.get("result") or {}
    leaked = [k for k in SECTIONS.values() if k != key and k in r]
    good = key in r and not leaked
    if not good:
        fails.append(f"sections={name} returned {sorted(r)}")
    print(f"  {'OK ' if good else 'BAD'} sections={name:<16} {key} present, leaked={leaked}")

_, job, _ = run("patient_id=1133&wait=true&sections=summary,goals")
r = job.get("result") or {}
combo = "planSummary" in r and "behavioralHealthGoals" in r and "concerns" not in r
if not combo:
    fails.append("sections=summary,goals returned the wrong set")
print(f"  {'OK ' if combo else 'BAD'} sections=summary,goals   both present, others absent")

code, _ = call("POST", "/api/patient?patient_id=1133&wait=true&sections=nonsense")
if code != 400:
    fails.append(f"unknown section -> {code}, wanted 400")
print(f"  {'OK ' if code == 400 else 'BAD'} sections=nonsense        {code} (want 400)")

print("\n-- date parameter validation --")
for q, want in (("start_date=2026-09-01&end_date=2026-09-30", 200),
                ("start_date=2026-09-01T10:00:00Z", 200),
                ("start_date=2026-09-01T10:00:00%2B05:30", 200),
                ("start_date=2026-09-01%2010:00:00", 200),
                ("start_date=2026/09/01", 400),
                ("start_date=09-01-2026", 400),
                ("start_date=2026-13-01", 400),
                ("start_date=2026-12-01&end_date=2026-01-01", 400)):
    code, _ = call("POST", f"/api/patient?patient_id=1133&wait=true&sections=summary&{q}")
    if code != want:
        fails.append(f"{q} -> {code}, wanted {want}")
    print(f"  {'OK ' if code == want else 'BAD'} {q:<46} {code} (want {want})")

print("\n-- async job flow --")
code, body = call("POST", "/api/patient?patient_id=1136")
job_id = body.get("jobId")
deadline, status = time.time() + 180, None
while time.time() < deadline:
    _, j = call("GET", f"/api/jobs/{job_id}")
    status = j.get("status")
    if status in ("done", "failed"):
        break
    time.sleep(1)
if status != "done":
    fails.append(f"async job ended as {status}")
print(f"  {'OK ' if status == 'done' else 'BAD'} POST without wait -> jobId, polled to {status}")

print("\n-- 4 concurrent (no single-job lock any more) --")
t0 = time.time()
with ThreadPoolExecutor(4) as pool:
    out = list(pool.map(lambda i: call("POST", f"/api/patient?patient_id=113{i}&wait=true", 180),
                        (3, 4, 5, 6)))
statuses = [c for c, _ in out]
fails.append(f"concurrent statuses {statuses}") if any(c != 200 for c in statuses) else None
print(f"  statuses={statuses}  wall={time.time()-t0:.1f}s")

print()
print("=" * 62)
if fails:
    print(f"  {len(fails)} FAILED")
    for f in fails:
        print(f"    {f}")
else:
    print("  all endpoint checks passed")
print("=" * 62)
sys.exit(1 if fails else 0)
