# Tests

Three layers, and the rule is which one you must run for which change.

| | command | needs | time |
|---|---|---|---|
| **Components** | `python tests/test_units.py` | nothing | ~1s |
| **Cold login** | `python tests/test_login.py` | credentials, a free TOTP window | ~40s |
| **End to end** | `python tests/test_e2e.py <base-url>` | a running instance | ~2min |

## Before pushing — automatic

```bash
git config core.hooksPath backend/scripts     # once per clone
```

After that every `git push` runs the component tests, and **refuses the
push if they fail**. If the change touches any of `login.py`,
`browser_pool.py`, `api_session.py`, `token_source.py`,
`patient_search.py` or `care_plan.py`, it also forces the cold-login gate.

That list is not arbitrary: it is the code that runs *only when there is
no session*. A warm process, a green unit run and a passing e2e suite all
say nothing about it — a container that has already logged in keeps
working while the code that logs in is broken, and you find out at the
next restart. Both outages looked exactly like that.

`git push --no-verify` skips it, for documentation-only changes.

## Deploying — verified, with rollback

On the server:

```bash
cd ~/Tross && bash backend/scripts/deploy.sh
```

It tags the running image as `:previous`, builds, starts the new one,
**waits for a real login to complete**, runs the endpoint suite against
it, and restores `:previous` if either fails.

This is the part a local test cannot give you. A fresh container has no
session, so reaching `hasToken: true` means login, MFA, department
selection, the app shell and token acquisition all worked *on the machine
that actually matters* — different CPU, different memory pressure,
different network. The deploy has always been the real cold-login test.
What was missing was anyone checking the result, and any way back.

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
