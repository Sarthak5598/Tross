"""Component tests. No network, no browser, no athenahealth.

Runs in about a second, so there is no excuse for pushing without it.

Every case here corresponds to something that actually broke in this
project. The two production outages both came from code paths that had no
test at all, so the rule this file exists to enforce is: if a bug was
worth fixing, it is worth pinning.

    python tests/test_units.py
"""

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED, FAILED = [], []


def check(name, actual, expected):
    if actual == expected:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}\n      expected: {expected!r}\n      actual:   {actual!r}")


def check_true(name, value):
    check(name, bool(value), True)


def raises(name, fn, exc=Exception):
    try:
        fn()
    except exc:
        PASSED.append(name)
        return
    except Exception as other:
        FAILED.append(f"{name}\n      raised {type(other).__name__}, wanted {exc.__name__}")
        return
    FAILED.append(f"{name}\n      did not raise {exc.__name__}")


def section(title):
    print(f"\n{title}")


def jwt(ttl_seconds, extra=None):
    """A realistically shaped JWT. The header matters: redaction matches on
    the real `eyJ...` prefix and length, so a toy header would let the
    redaction test pass against a string no real token resembles."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = {"exp": time.time() + ttl_seconds}
    payload.update(extra or {})
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.aGVyZUlzQVNpZ25hdHVyZQ"


# ---------------------------------------------------------------- redaction
section("redaction — a leaked password is why this module exists")
import config
from automation.redact import redact

leaky = (
    'Page.fill: Timeout 30000ms exceeded.\n'
    '  - locator resolved to <input disabled type="password" '
    f'id="athena-password" value="{config.ATHENA_PASSWORD}" '
    'placeholder="enter password" aria-label="Password"/>\n'
    '  - fill("933484")\n' + "x" * 900
)
scrubbed = redact(leaky)
check_true("password removed", config.ATHENA_PASSWORD not in scrubbed)
check_true("username removed", config.ATHENA_USERNAME not in scrubbed)
check_true("totp secret removed", config.ATHENA_TOTP_SECRET not in scrubbed)
check_true("value= attribute scrubbed", 'value="[redacted]"' in scrubbed)
check_true("placeholder scrubbed", 'placeholder="[redacted]"' in scrubbed)
check_true("aria-label scrubbed", 'aria-label="[redacted]"' in scrubbed)
check_true("output truncated", len(scrubbed) < len(leaky))
check_true("unknown field also scrubbed",
           'value="[redacted]"' in redact('<input value="some-future-secret">'))
check_true("jwt scrubbed", "eyJ" not in redact(f"Authorization: Bearer {jwt(300)}"))
check("empty input survives", redact(""), "")
check("none-ish input survives", redact(None), None)


# ------------------------------------------------------------------- dates
section("date handling — three formats silently produced garbage")
from automation.runner import _normalise_dates

check("plain date -> start of day",
      _normalise_dates("2026-09-01", "2026-09-30")[2], "2026-09-01T00:00:00.000Z")
check("bare end date means END of that day",
      _normalise_dates("2026-09-06", "2026-09-06")[3], "2026-09-06T23:59:59.999Z")
check("offset converted to UTC, not appended",
      _normalise_dates("2026-09-01T10:00:00+05:30", "2026-09-30")[2],
      "2026-09-01T04:30:00.000Z")
check("space separator accepted",
      _normalise_dates("2026-09-01 10:00:00", "2026-09-30")[2], "2026-09-01T10:00:00.000Z")
check("compact form normalised",
      _normalise_dates("20260901", "2026-09-30")[0], "2026-09-01")
check("millis preserved",
      _normalise_dates("2026-09-01T10:00:00.500Z", "2026-09-30")[2],
      "2026-09-01T10:00:00.500Z")
check("defaults cover everything",
      _normalise_dates(None, None)[0:2], ("2018-01-01", "2099-12-31"))
raises("slashes rejected", lambda: _normalise_dates("2026/09/01", "2026-09-30"), ValueError)
raises("us order rejected", lambda: _normalise_dates("09-01-2026", "2026-09-30"), ValueError)
raises("month 13 rejected", lambda: _normalise_dates("2026-13-01", "2026-09-30"), ValueError)
raises("reversed range rejected", lambda: _normalise_dates("2026-12-01", "2026-01-01"), ValueError)


# ------------------------------------------------------------- departments
section("departments — three input forms must all resolve")
from automation.departments import Department, resolve, catalog

check("code", resolve("SH_OH_SHAKER"), Department.SH_OH_SHAKER)
check("code is case-insensitive", resolve("sh_oh_shaker"), Department.SH_OH_SHAKER)
check("display label still accepted", resolve("SH OH - Shaker"), Department.SH_OH_SHAKER)
check("numeric athena id as string", resolve("4"), Department.SH_OH_SHAKER)
check("numeric athena id as int", resolve(4), Department.SH_OH_SHAKER)
check("a different numeric id", resolve(15), Department.SH_OH_NORTH_CANTON)
check("none means unset", resolve(None), None)
check("empty means unset", resolve(""), None)
raises("partial name rejected", lambda: resolve("shaker"), ValueError)
raises("unknown id rejected", lambda: resolve("99"), ValueError)
check("catalog lists all eight", len(catalog()), 8)
check_true("catalog rows carry code, label and id",
           all({"code", "label", "athenaId"} <= set(row) for row in catalog()))


# ----------------------------------------------------------- plan selection
section("plan selection — reported 14 goals where the live plan had 5")
from automation.extract_gql import select_concerns, TREATMENT_PLAN_TYPE, CARE_PLAN_TYPE

plans = [
    {"Id": "live", "HealthConcernType": TREATMENT_PLAN_TYPE, "IsArchived": False},
    {"Id": "old1", "HealthConcernType": TREATMENT_PLAN_TYPE, "IsArchived": True},
    {"Id": "old2", "HealthConcernType": TREATMENT_PLAN_TYPE, "IsArchived": True},
    {"Id": "care", "HealthConcernType": CARE_PLAN_TYPE, "IsArchived": False},
]
ids = lambda rows: [r["Id"] for r in rows]
check("default keeps only the live treatment plan",
      ids(select_concerns(plans, False, False)), ["live"])
check("include_archived adds the superseded ones",
      ids(select_concerns(plans, False, True)), ["live", "old1", "old2"])
check("include_care_plan adds the longitudinal one",
      ids(select_concerns(plans, True, False)), ["live", "care"])
check("both flags return everything",
      ids(select_concerns(plans, True, True)), ["live", "old1", "old2", "care"])
check("no concerns is not an error", select_concerns([], False, False), [])
check("an archived care plan needs both flags",
      ids(select_concerns([{"Id": "x", "HealthConcernType": CARE_PLAN_TYPE,
                            "IsArchived": True}], True, True)), ["x"])


# -------------------------------------------------------- graphql responses
section("graphql errors — upstream reports 404 as HTTP 200")
from automation.graphql import (CareManagementClient, GraphQLError, PatientNotFound,
                                InvalidRequest, _summarise, NOT_FOUND_MARKERS)

check("MDP_ERROR line extracted from a stacktrace",
      _summarise([{"message": 'Unexpected error value: { CODE: 404, ERROR: "Not Found", '
                              'MDP_ERROR: "The Patient ID or Department ID is invalid." }',
                   "extensions": {"stacktrace": ["noise"] * 60}}]),
      "The Patient ID or Department ID is invalid.")
check("message line extracted when there is no MDP_ERROR",
      _summarise([{"message": 'Unexpected error value: { message: '
                              '"request/params/patientId must be integer" }'}]),
      "request/params/patientId must be integer")
check("empty errors array does not crash", _summarise([]), "unknown GraphQL error")
check_true("both not-found wordings are covered",
           "CODE: 404" in NOT_FOUND_MARKERS and "does not exist" in NOT_FOUND_MARKERS)
check_true("prose-only not-found is matched",
           any(m in "The specified patient does not exist in that department."
               for m in NOT_FOUND_MARKERS))

client = CareManagementClient({"authorization": "Bearer x", "x-athena-context": "1",
                               "host": "dropme", "content-length": "9"})
check_true("transport headers dropped", "host" not in client.headers)
check_true("content-length dropped", "content-length" not in client.headers)
check_true("auth header kept", "authorization" in client.headers)
check("for_department swaps only the department header",
      client.for_department(15).headers.get("x-athena-department"), "15")
check_true("for_department leaves the original alone",
           "x-athena-department" not in client.headers)


# -------------------------------------------------------------- error types
section("error classification — every failure needs a defined status")
from automation.runner import classify_error
from automation.patient_search import PatientNotFoundError, PatientRecordMismatchError

check("upstream not-found -> 404 type",
      classify_error(PatientNotFound("gone"))[0], "patient_not_found")
check("browser not-found -> 404 type",
      classify_error(PatientNotFoundError("gone"))[0], "patient_not_found")
check("bad argument -> 400 type",
      classify_error(InvalidRequest("bad"))[0], "invalid_request")
check("unknown department -> 400 type",
      classify_error(ValueError("Unknown department"))[0], "invalid_request")
check("record mismatch -> 409 type",
      classify_error(PatientRecordMismatchError("other group"))[0], "patient_record_mismatch")
check("network failure -> 503 type",
      classify_error(Exception("net::ERR_CONNECTION_REFUSED"))[0], "site_unavailable")
check("anything else -> 500 type",
      classify_error(Exception("something odd"))[0], "automation_error")
check_true("classified messages are redacted too",
           config.ATHENA_PASSWORD not in
           classify_error(Exception(f'value="{config.ATHENA_PASSWORD}"'))[1])

from main import STATUS_FOR_ERROR_TYPE
for error_type in ("patient_not_found", "invalid_request", "patient_record_mismatch",
                   "site_unavailable", "timeout", "automation_error"):
    check_true(f"{error_type} has an http status", error_type in STATUS_FOR_ERROR_TYPE)
check("not-found is 404", STATUS_FOR_ERROR_TYPE["patient_not_found"], 404)
check("invalid request is 400", STATUS_FOR_ERROR_TYPE["invalid_request"], 400)


# ------------------------------------------------------------- idempotency
section("idempotency — every parameter must be part of the key")
import jobs

base = ("1133", None, None, False, False, False, None, None)
key = jobs.make_cache_key(*base)
check("identical requests share a key", jobs.make_cache_key(*base), key)
variants = {
    "patient_id": ("1134", None, None, False, False, False, None, None),
    "sections": ("1133", {"goals"}, None, False, False, False, None, None),
    "department": ("1133", None, "SH_OH_SHAKER", False, False, False, None, None),
    "include_history": ("1133", None, None, True, False, False, None, None),
    "include_care_plan": ("1133", None, None, False, True, False, None, None),
    "include_archived": ("1133", None, None, False, False, True, None, None),
    "start_date": ("1133", None, None, False, False, False, "2026-01-01", None),
    "end_date": ("1133", None, None, False, False, False, None, "2026-12-31"),
}
for name, args in variants.items():
    check_true(f"{name} changes the key", jobs.make_cache_key(*args) != key)
check("section order does not matter",
      jobs.make_cache_key("1133", {"goals", "summary"}, None),
      jobs.make_cache_key("1133", {"summary", "goals"}, None))


# ------------------------------------------------------------ token manager
section("token manager — renewal must never leave us holding nothing")
import asyncio
from automation.token_manager import TokenManager, decode_expiry, RENEW_MARGIN_S

check("expiry read from a jwt",
      round(decode_expiry(jwt(300)) - time.time()), 300)
check("non-jwt returns none", decode_expiry("not-a-jwt"), None)


async def token_tests():
    # a slow acquisition must not blank the token we are still serving
    async def slow(min_expiry=0.0):
        await asyncio.sleep(0.4)
        return jwt(300), "ctx"

    manager = TokenManager(slow)
    manager._token, manager._context = jwt(60), "ctx"
    manager._expires_at = time.time() + 60

    task = asyncio.create_task(manager._renew())
    await asyncio.sleep(0.2)
    check_true("token still served mid-renewal", manager.status()["hasToken"])
    served, _ = await manager.get()
    check_true("a request mid-renewal is answered immediately", bool(served))
    await task
    check_true("token replaced once acquired", manager.status()["secondsRemaining"] > 250)

    # a replacement that is not newer must be refused, or the loop spins
    stale = jwt(90)
    async def returns_stale(min_expiry=0.0):
        return stale, "ctx"

    manager2 = TokenManager(returns_stale)
    manager2._token, manager2._expires_at = stale, decode_expiry(stale)
    try:
        await manager2._renew()
        FAILED.append("stale replacement must be rejected\n      it was accepted")
    except RuntimeError:
        PASSED.append("stale replacement rejected")

    # concurrent callers trigger exactly one acquisition (TOTP is single-use)
    calls = {"n": 0}
    async def counted(min_expiry=0.0):
        calls["n"] += 1
        await asyncio.sleep(0.3)
        return jwt(300), "ctx"

    manager3 = TokenManager(counted)
    await asyncio.gather(*(manager3.get() for _ in range(10)))
    check("ten concurrent callers cause one acquisition", calls["n"], 1)

    # renewal margin must exceed a realistic acquisition
    check_true("renewal margin leaves room for a slow acquisition", RENEW_MARGIN_S >= 150)

    # invalidate is for dead tokens only, and must actually clear
    manager4 = TokenManager(slow)
    manager4._token, manager4._expires_at = jwt(300), time.time() + 300
    await manager4.invalidate()
    check_true("invalidate clears the token", not manager4.status()["hasToken"])

asyncio.run(token_tests())


# --------------------------------------------------------------- mapping
section("mapping — assembles the response from upstream payloads")
from automation.extract_gql import build_result

plan = {"getPatientCarePlanInternal": {"PatientId": "1133", "HealthConcerns": [
    {"Id": "1", "Name": "Treatment Plan created on 01-01-2026",
     "HealthConcernType": TREATMENT_PLAN_TYPE, "IsArchived": False,
     "ReviewDueDate": "2026-07-10T08:00:00.000Z", "AddedBy": "A Clinician",
     "Problems": [{"Id": "p1", "Description": "Anxiety", "ConcernStatus": "Achieved"}],
     "Goals": [{"Id": "g1", "Name": "Manage anxiety", "LifecycleStatus": "active",
                "AchievementStatus": "improving", "Modalities": [{"Name": "CBT"}]}]},
    {"Id": "2", "Name": "Care Plan created on 02-01-2026",
     "HealthConcernType": CARE_PLAN_TYPE, "IsArchived": False,
     "Problems": [], "Goals": []},
]}}
schedules = {"1": [
    {"TaskSchedule": {"Id": "t1", "GoalIds": ["g1"], "TaskStatus": "in-progress",
                      "AssignedTask": {"Name": "An objective", "PatientAssignable": True}}},
    {"TaskSchedule": {"Id": "t2", "GoalIds": ["g1"], "TaskStatus": "in-progress",
                      "AssignedTask": {"Name": "An intervention", "PatientAssignable": False}}},
]}

result = build_result(plan, schedules, None, None, None, None, False, False)
check("care plan excluded by default", result["planScope"]["returned"], 1)
check("planScope reports the total on record", result["planScope"]["totalOnRecord"], 2)
check("planScope names what was excluded", result["planScope"]["excludedCarePlan"], 1)
check("one goal returned", len(result["behavioralHealthGoals"]), 1)
goal = result["behavioralHealthGoals"][0]
check("objectives split by PatientAssignable", [o["title"] for o in goal["objectives"]],
      ["An objective"])
check("interventions are the rest", [i["title"] for i in goal["interventions"]],
      ["An intervention"])
check("plan summary is tagged with its type",
      result["planSummary"][0]["plan_type"], "Treatment Plan")
check_true("plan summary records archived state",
           result["planSummary"][0]["is_archived"] is False)

with_care = build_result(plan, schedules, None, None, None, None, True, False)
check("include_care_plan returns both plans", with_care["planScope"]["returned"], 2)

only_summary = build_result(plan, schedules, None, None, None, {"summary"}, False, False)
check_true("sections filter drops goals", "behavioralHealthGoals" not in only_summary)
check_true("sections filter keeps what was asked for", "planSummary" in only_summary)

second_concern = json.loads(json.dumps(plan))
second_concern["getPatientCarePlanInternal"]["HealthConcerns"][1].update(
    {"HealthConcernType": TREATMENT_PLAN_TYPE,
     "Goals": [{"Id": "g2", "Name": "A goal on the second concern"}]})
both = build_result(second_concern, schedules, None, None, None, None, False, False)
check("goals under a second concern are not lost", len(both["behavioralHealthGoals"]), 2)


# ------------------------------------------------------------------- report
print()
print("=" * 62)
print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print()
    for failure in FAILED:
        print(f"  FAIL  {failure}")
print("=" * 62)
sys.exit(1 if FAILED else 0)
