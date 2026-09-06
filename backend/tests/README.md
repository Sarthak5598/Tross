# Tests

Three layers, and the rule is which one you must run for which change.

| | command | needs | time |
|---|---|---|---|
| **Components** | `python tests/test_units.py` | nothing | ~1s |
| **Cold login** | `python tests/test_login.py` | credentials, a free TOTP window | ~40s |
| **End to end** | `python tests/test_e2e.py <base-url>` | a running instance | ~2min |

## Before pushing

Always: `python tests/test_units.py` — 97 checks, no network, no excuse.

**Also run the cold-login gate if you touched any of:**
`login.py`, `browser_pool.py`, `api_session.py`, `token_source.py`,
`patient_search.py`, `care_plan.py`.

This is not optional caution, it is the lesson from two outages. Both came
from the login path, which runs *only when there is no session* — so a
warm process, a green unit run and a passing e2e suite all say nothing
about it. A container that has already logged in keeps working while the
code that logs in is broken, and you find out at the next restart.

The gate throws the session away and logs in for real. It is the only
thing that would have caught either failure.

## After deploying

```bash
python tests/test_e2e.py http://52.91.250.2:8000
```

Give a fresh container ~40s first — it has to complete a real login before
`hasToken` goes true.

Token renewal is the one thing no suite covers, because it happens once
every ~80 seconds. Watch it directly:

```bash
for i in $(seq 1 30); do curl -s http://52.91.250.2:8000/health \
  | grep -o '"secondsRemaining":[0-9]*' | cut -d: -f2; sleep 7; done
```

The number should never reach 0 outside cold start, and should turn over
around 220 rather than scraping single digits.

## Why every wait in this suite is bounded

An earlier ad-hoc harness polled a job id that was never created and spun
for over an hour. A test that cannot fail is worse than no test.
