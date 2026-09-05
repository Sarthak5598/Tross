# Troubleshooting Log

Running record of real issues hit while building this, why they happened, and how they were fixed. Kept so we don't re-debug the same thing twice.

## 12. Automation felt much slower than clicking around manually

**Symptom:** User noted the API "feels slow" compared to just clicking through the sandbox manually, plus wanted smaller responses and no concurrent jobs.

**Root cause:** Two real, separate issues:
- Every request launched a brand-new Chromium process (~12-15s) AND redid the entire login → MFA → department-selection chain (~34s in a measured run) — every single time. A human logs in once and clicks around; automation was logging in fresh on every request. This was the single biggest chunk of the perceived slowness.
- The full extraction response always included every section (Concerns, Summary, Attestations, Goals with nested Objectives/Interventions/Progress, Client Characteristics) regardless of what the caller actually needed.

**Fix:**
- `automation/browser_pool.py`: keeps one Chromium process and one authenticated `Page` alive across requests (started/stopped via FastAPI startup/shutdown events in `main.py`). Before each job, `is_logged_in(page)` runs the same app-shell readiness check `login.py` already used (`AH.Frames.Top.Frame().navsearchobject`) — only if that fails (session actually expired/dead) does it redo the full login chain. The persistent page is never closed between jobs.
- `jobs.py`: added `try_start`/`release`/`active_job_id` — a simple one-job-at-a-time lock. `main.py` reserves the slot before scheduling a job and returns `409` with the currently-running job's id if one is already in flight, rather than letting two jobs share the one persistent browser/session concurrently (which would also risk re-triggering the sandbox's known MFA-throttling behavior from TROUBLESHOOTING.md #8).
- `/api/care-plan` now takes an optional `sections` query param (subset of `summary,attestations,concerns,goals,characteristics`) — only those sections are extracted and returned. Omit it for the old full-response behavior.

**Confirmed fixed:** Two consecutive `/api/care-plan` calls against patient 1133 (`sections=concerns`) — job 1 (fresh session): 80.2s total, ~34s of which was the login chain. Job 2 (reused session): 20.5s total, `[0.00s] Reusing existing authenticated session` — no login steps at all.

**Remaining real (non-artificial) cost:** even with a warm session, the Care Management/Treatment Plan pane itself took ~10-20s to render in both runs — this is genuine shadow-DOM widget load time in headless Chromium, not a fixed/artificial wait in our code (the wait is condition-based on `GOAL_ITEM_SELECTOR` appearing, not a flat sleep). Not yet investigated further.

## 13. Multi-department support — switching without a full re-login

**Need:** The sandbox account has 8 departments; the persistent-session model (see #12) picks a department once at first login and stays there, so a request for a patient in a different department would silently search the wrong context.

**Key fact (confirmed by user manually navigating there):** revisiting `choosedepartment.esp` on an already-authenticated session shows "Last login: ..." and the department dropdown again — it does NOT require re-entering username/password/MFA. So switching departments is just: `page.goto` the same choosedepartment URL, `select_option` a different label, click Go, wait for the app shell to reload.

**Implementation:**
- `login()` in `automation/login.py` now captures and returns `department_page_url` (the exact URL landed on right after MFA) and the pre-selected default department's label, instead of blindly clicking "Go" and discarding that info.
- `select_department(page, department_page_url, department_label, on_step)` re-visits that URL, selects the target department by its visible label (not by an internal value/code — labels are user-readable and we don't have a stable code mapping), submits, and waits for the app shell to be ready again (same check as the tail of `login()`, factored into `_wait_for_app_shell`).
- `automation/browser_pool.py` tracks `_department_page_url` and `_current_department` across jobs (set once via `record_login()` after any fresh login) and exposes `ensure_department(page, department, on_step)` — a no-op if the requested department is `None` or already active, otherwise switches and updates the tracked state.
- `/api/care-plan` and `/api/patient-search` both take an optional `department` query param (the exact visible label) which flows through `runner.py` into `ensure_department`.

**Confirmed fixed:** Two-part test. First, requesting the *same* department that's already active correctly no-op'd (skipped the switch entirely, went straight to search). Second, forcing an actual switch with a deliberately unknown department label correctly reused the session, navigated to `choosedepartment.esp`, attempted `select_option`, and failed with a clean error listing the **real 8 department labels** for this sandbox account:
`IPC TN - Ascension St. Thomas Midtown`, `IPC TN - Ascension St. Thomas River Park`, `IPC TN - Ascension St. Thomas West`, `IPC TN - HCA Centennial`, `SH OH - North Canton`, `SH OH - Shaker`, `SH OH - West Cleveland`, `SH TN - Patterson`.

**Not yet tested:** an actual switch to a *real, different* department (only the no-op path and the deliberate-failure path have been proven so far) — still need a patient ID known to be in one of the other 7 departments to confirm data actually differs / the switch fully works end-to-end with real data on the other side.

## 14. Merged /api/patient-search + /api/care-plan into one endpoint

**Need:** The two endpoints each independently redid login+search for what was really one flow — calling both for the same patient meant paying that cost twice. User asked for a single endpoint with query params controlling response depth instead.

**Fix:**
- `automation/care_plan.py`: `extract_behavioral_health_goals(page, on_step, expand=True)` — `expand=False` skips `_expand_goal` and `_extract_expanded_goal_details` entirely per goal, returning only the fields that live on the goal's own list item (status, priority, title, statement, targeted concerns, dates, attribution) without touching Baseline/Treatment Modalities/Objectives/Interventions/Goal Progress History (the shared-panel fields, and the slowest part of the whole flow — see #12).
- `automation/runner.py`: `run_patient_job(job_id, patient_id, sections=None, department=None, shorter=False)` replaces the old separate `run_patient_search_job` and `run_care_plan_job`. Empty `sections` (`set()`) stops right after patient search — no Care Management navigation at all (the old patient-search behavior). `shorter=True` passes `expand=False` down into goal extraction.
- `main.py`: single `POST /api/patient` replaces `/api/patient-search` and `/api/care-plan`. Params: `sections` (comma-separated subset of `summary,attestations,concerns,goals,characteristics`; omit for all, pass empty string for patient-lookup-only), `shorter` (bool, default `False` — explicitly opt-in, so omitting it keeps today's full-detail behavior unchanged), `department` (optional label, see #13).
- `streamlit_app.py` updated to match: one "Patient lookup" mode with a sections-mode radio (All / patient-found-only / custom) and a `shorter` checkbox.

**Confirmed fixed:** Two real runs against patient 1133. `sections=set()` → `result keys: []`, stopped right after "Patient record loaded", no Care Management pane opened at all. `sections={'goals'}, shorter=True` → 40.4s total (with a reused session), first goal's keys were exactly `['status', 'priority', 'title', 'client_statement', 'targeted_concerns', 'start_date', 'review_date', 'target_date', 'added_by', 'last_action_by']` — none of the nested fields present.

## 15. Idempotency: two callers requesting the same patient at once

**Need:** If two people (or a retried request) call `/api/patient` for the same patient+params close together, both should get the result — not have the second one rejected outright, and not trigger two separate automation runs for the same thing.

**Fix:** `jobs.py` added a short-window (`IDEMPOTENCY_TTL_SECONDS = 60`) in-memory cache keyed by `(patient_id, sections, department, shorter)`. `main.py`'s `/api/patient` checks this cache **before** reserving the exclusive job slot: a cache hit (job still running, or done and not failed) returns the same `jobId` immediately (`"deduped": true`); a miss reserves a new job and caches it. A failed cached job is invalidated rather than handed back. This is a short dedup window against duplicate/retried calls, not the separate long-lived "freshness" DB cache the stakeholder was explicit must still hit Athena live every real request.

**Confirmed fixed:** Spun up a temporary second server instance (port 8011, so as not to disturb the user's already-running one on 8010) and fired two near-simultaneous identical `POST /api/patient?patient_id=1133&sections=` requests — both returned the exact same `jobId`, one `"deduped": false"` (the original) and one `"deduped": true` (piggybacked). A third, genuinely different request (`patient_id=9999`) fired while the first was still running correctly got `409` (not falsely deduped), since it's a different cache key and the single-job lock was already held.

**Caveat for later (deployment):** this dedup logic relies on there being no `await` between the cache-check and cache-write inside the route handler, which makes it atomic under a single asyncio event loop — but it's all in-process memory, so it stops being correct the moment the app runs as more than one worker process (each process would have its own cache/lock). Worth remembering when deployment comes up.

## 16. Speed pass: one optimization caught as a real regression before shipping

**Context:** User asked for further speed ideas (full flow was ~60s+ even with a warm session). Three changes were attempted together:
1. Parallelize the 4 independent, read-only section extractions (Summary/Attestations/Concerns/Client Characteristics) via `asyncio.gather` instead of sequential `await`s — safe, since none of them click anything or share mutable UI state.
2. Make per-step screenshot capture opt-in (`live` param, default `False`) instead of always-on — every screenshot is real per-step overhead a plain API caller gets no benefit from.
3. Replace the flat 4s per-goal wait (in `_extract_expanded_goal_details`) with a poll: capture the Baseline description before expanding a goal, then poll until it changes from that previous value, capped at the same 4s as a safety net.

**#3 was a real regression, caught before shipping:** a live test showed all 6 goals coming back with `0 objectives, 0 interventions` while `baseline_description` was still populated correctly for every one. The tell: all 6 "Expanded a goal panel" steps landed within ~0.2s of each other — the poll was exiting almost instantly every time. Root cause: the assumption that "Baseline description changing" means the whole shared detail panel (including Objectives/Interventions) is ready was never actually verified, and turned out false — Baseline populates faster than Objectives/Interventions, so polling on Baseline alone reads stale/empty Objectives every time.

**Fix:** Reverted #3 back to the flat `page.wait_for_timeout(4000)`, which is the version already confirmed correct against real data (see #10). Kept #1 and #2, which don't touch this timing at all. A genuinely faster per-goal wait needs a readiness signal specific to Objectives/Interventions themselves (or the network-interception idea already flagged as a separate future investigation) — not attempted again without one.

**Not yet fully re-verified:** the follow-up timed re-run (to confirm the revert restored correct data and to measure the actual speedup from #1+#2) got stuck for 20+ minutes with zero progress — almost certainly the same rapid-login sandbox throttling documented in #8, from the sheer number of back-to-back automated logins run across this session. It was killed rather than left waiting indefinitely. Per the user's choice, further verification of this speed pass is deferred to their own manual testing pass rather than more automated retries right now. The code as it stands: parallel section reads + opt-in screenshots are live; the per-goal wait is back to the known-good flat 4s delay, unchanged from before this session's speed pass.

## 17. A hung job took down the whole service (plus memory, error-clarity and timeout fixes)

**Symptom:** A dashboard run failed with a bare `Locator.click: Timeout 30000ms exceeded ... waiting for get_by_text("Go to Treatment Plan")`. Investigating that surfaced several deeper problems.

**17a. One hung job = full outage.** Only one job runs at a time (see #12), and there was no ceiling on how long a job could take — so a hang held the lock forever and every subsequent request got `409` until the process was restarted. This was not theoretical: two 20+ minute stalls were observed in one session. Fixed with `JOB_TIMEOUT_S = 300` wrapping the whole job in `asyncio.wait_for` in `_run_browser_job`; on timeout the job is failed with an actionable message, `jobs.release()` still runs in the `finally`, and `browser_pool.reset_page()` drops the (likely wedged mid-navigation) shared page so the next job starts clean rather than inheriting it. Verified with a stubbed hang: job fails, lock releases, next job starts.

**17b. Unbounded memory growth.** `frames._store` and `jobs._jobs` never evicted — every job's JPEG frames stayed resident for the life of the process. Fine locally, a steady leak on an always-on server. `frames.py` now caps at `MAX_FRAMES_PER_JOB = 60` and `MAX_JOBS_RETAINED = 20`; `jobs.py` caps at `MAX_RETAINED_JOBS = 100`. Frames are now keyed by a monotonically increasing index rather than list position, specifically so evicting old frames doesn't renumber the rest — `count()` still returns "frames ever captured", so a viewer asking for `count - 1` always gets the newest one, and evicted indices just return None (already a 404 path). Verified: 150 frames captured → 60 retained, newest still fetchable at index 149, index 0 returns None.

**17c. The 30s that came from nowhere.** Every deliberate wait in `open_care_management_pane` used our own 15s/45s constants, but `frame.get_by_text(...).click()` passed no timeout at all and so silently inherited Playwright's 30s default — hence the mismatched number in the error. Fixed twice over: that click (and the Quickview click) now pass `NAV_TIMEOUT_MS` explicitly, and `browser_pool.ensure_page()` calls `page.set_default_timeout(20_000)` so any *other* call that omits a timeout is consistent with the rest instead of silently getting 30s.

**17d. Undiagnosable failure.** That link only renders when the patient has a Treatment Plan **in the currently active department** — so the real cause is usually department scoping, but the error said nothing about it. The click is now wrapped to raise a message naming the likely cause and the active department, and `jobs.set_department()` records the department on every job so this is diagnosable after the fact.

**Underlying gotcha worth remembering:** the department-choice page pre-selects *whatever department last logged in on that account*, so "the default department" is not a fixed value — it drifts as different runs log in. Relying on it is fragile; pass `department` explicitly (or pin one in `.env`) rather than trusting the account default.

## 18. Second failed attempt at a faster per-goal wait — stop optimizing this

**Attempt:** Replace the flat 4s wait with `page.expect_response()` around the "Show More" click, waiting for the goal-detail GraphQL call (`getTaskSchedulesWithScheduledTasks`, measured at 2.6-3.9s in #16's investigation), then a 500ms render settle — with a fallback to the old flat wait if the response never arrived. Predicate matched on the GraphQL **operation name in `request.post_data`**, since all of this widget's calls share one URL and an `expect_response` predicate can't await a body.

**Result: regressed, worse than the previous attempt.** Every goal failed to expand — `Could not expand goal N: Timeout 10000ms exceeded ... waiting for "Show Less"` — so all six came back with 0 objectives/0 interventions. Titles/status/dates still extracted fine, which is exactly what makes this failure mode dangerous: the response looks structurally complete.

**Two things went wrong, one of them a genuine coding defect:**
- The `try/except Exception` wrapped the *entire* `async with expect_response(...)` block, which contains the click. So a failing click was silently swallowed and misreported as "didn't see the response" instead of surfacing. Never wrap a triggering action in a broad except purely to catch the waiter's timeout.
- Per-goal elapsed time was ~10.1s (exactly `EXPAND_TIMEOUT_MS`), not ~18s (8s response timeout + 10s), meaning `expect_response` returned almost immediately rather than timing out — likely matching an in-flight response left over from the pane load rather than one caused by our click. Root cause not fully pinned down before reverting.

**Reverted** to the flat `GOAL_DETAIL_WAIT_MS = 4_000`, the only version ever verified clean against real data (#10).

**Standing recommendation: leave this alone.** Two independent attempts (Baseline polling in #16, response waiting here) have both produced silent, total data loss on the nested goal fields, and the measured upside is only ~3-4s per run against a ~2.6-3.9s server-side floor we don't control. The risk/reward is bad. If it's ever revisited, the non-negotiable is a full re-verification against the known-good counts in #10 — every attempt so far *looked* fine and was only caught by checking those numbers.

## 19. Post-MFA login timeout — the "load" event bug, again

**Symptom:** Login failed right after "Submitted MFA form":
```
Timeout 15000ms exceeded.
waiting for navigation to "://preview.athenahealth.com/" until 'load'
```

**Root cause:** `until 'load'` is the tell. `page.wait_for_url()` defaults to waiting for the **load** event, and this legacy app's `load` frequently never fires — background polling / long-lived requests keep it pending indefinitely. That is the exact same root cause already documented in #9, which is why `open_care_management_pane` passes `wait_until="domcontentloaded"` explicitly. The login step never got the same treatment, so it had been quietly relying on `load` happening to fire in time. Under slower conditions it doesn't, and the step times out even though the URL had already matched and the page was perfectly usable.

**Fix:** `page.wait_for_url(..., wait_until="domcontentloaded")` in `login.py`. Audited the rest of the codebase — this was the only remaining navigation wait using the implicit `load` default.

**Correction to #8:** several earlier failures in this session that stalled right after "Submitted MFA form" were attributed to rapid-login throttling. Any of those that failed at *exactly* the 15s timeout were almost certainly this bug instead, not throttling. (The much longer 10-20 minute hangs were elsewhere in the flow and remain unexplained.)

**Lesson:** in this app, assume `load` never fires. Any navigation wait must specify `wait_until="domcontentloaded"` — the default will work right up until it doesn't.

## 20. Nonexistent patient ID wasn't handled at all

**Symptom:** Searching a patient ID that doesn't resolve (e.g. 1333) left the job sitting at "Submitted patient search" until the 15s selector timeout, then failed with a generic Playwright error — while athena was plainly rendering `Patient #1333 does not exist, or you do not have permission to view this record.` on screen the whole time.

**Root cause:** `search_patient` only ever waited for the success signal (`.pb_c_patient-id-module`). The failure case was never checked, so a perfectly clear message from the app was ignored in favour of a timeout.

**Fix:** Race both outcomes with `loaded.or_(not_found).first.wait_for(...)`, then report whichever actually happened. Raises a `PatientNotFoundError` naming the patient and the active department. Also added an `errorType` field on jobs (`patient_not_found` / `timeout` / `automation_error`) so a caller can distinguish bad input (fix the request) from a broken run (retryable) without string-matching the message.

**Note on the wording:** athena deliberately conflates "no such patient" with "you lack permission to view it" in one message, so we can't tell them apart either — the error says so rather than guessing. Since visibility is department-scoped (see #13/#17), the message points at `department` as the likely culprit.

**Confirmed fixed:** patient 1333 now fails in 0.56s from search submit (was: full 15s timeout), with `errorType='patient_not_found'` and a message naming department `'SH OH - Shaker'`.

## 21. Proven: goal detail panels are shared, so "expand all then read" is impossible

**Question:** Could we click "Show More" on every goal, then read them all in one pass — turning 6 sequential ~4.5s expand+read cycles into one?

**Answer: no**, and this is now measured rather than assumed. Probe results on the real 6-goal patient:

| State | `.acc_c_goal-expanded-view` in DOM | Objective/intervention titles visible |
|---|---|---|
| Before any expand | 0 | 0 |
| Expand goal 1 | 1 | goal 1's 6 items |
| Also expand goal 2 | **1** | goal 2's 5 items — goal 1's are **gone** |
| Expand goals 3,4,5 rapidly | **1** | only goal 5's 4 items |

There is exactly one `Objectives` card and one `Interventions` card in the DOM at any time (card-title count stays at 7 throughout). Expanding a second goal **overwrites** the first's contents rather than adding a panel. Expanding all six and then reading would yield only the last goal's data, with five silently empty — the same failure signature as #16 and #18.

This is the structural reason the per-goal loop must stay sequential, and it caps how fast this section can ever be with a DOM-based approach.

## 22. Stale patient page reported as loaded — a wrong-record risk

**Symptom:** A dashboard run for nonexistent patient 1333 logged `[1.66s] Patient record loaded` — 0.47s after submitting the search — and only failed later, clicking Quickview on a page that had no Quickview button. The not-found handling added in #20 never triggered.

**Root cause — worse than the missing error message.** `search_patient` inspected `frMain` immediately after pressing Enter, without waiting for the search to actually navigate. On a **reused session** (see #12) frMain still displays the *previous* patient, so `PATIENT_LOADED_SELECTOR` matched instantly against stale content and we declared success. The not-found text wasn't there yet because the new page hadn't loaded at all.

The generalisation is what matters: this wasn't only a bad-input bug. Any slow navigation could have us reading the **previous patient's record** and returning it as the requested one. For medical data that's a correctness failure far more serious than an unhelpful error.

It also explains why #20's standalone test passed: it used a *fresh* login, so there was no prior patient page to match. Session reuse was the missing variable — a reminder that anything touching the shared page must be tested on a reused session, not just a cold one.

**Fix (`patient_search.py`), two independent guards:**
- Wrap the Enter keypress in `patient_frame.expect_navigation(wait_until="domcontentloaded")` so we only inspect the frame after a real navigation, then re-fetch the frame handle (stale-handle risk, see `_wait_for_frame`).
- On the success path, assert the frame URL contains `ID=<patient_id>` before extracting. If it doesn't, refuse rather than scrape — better to fail than to return someone else's record.

**Confirmed fixed** by reproducing the exact conditions: valid 1133 (fresh login) → done; **1333 on the reused session with 1133 still displayed → `patient_not_found` in 1.4s** (previously "Patient record loaded"); 1133 again on the same session → done, happy path intact.

## 23. HTTP status codes / handling athena being down

**Gap:** Because jobs run as FastAPI `BackgroundTasks`, `POST /api/patient` returned `200 {jobId}` before anything could fail — so a caller could never receive a meaningful status code, whatever went wrong.

**Fix:**
- `runner.classify_error()` maps an exception to an `errorType`: `patient_not_found`, `site_unavailable` (Chromium `net::ERR_*` navigation failures — athena unreachable), `timeout` (our own `JOB_TIMEOUT_S` ceiling), or `automation_error`.
- `main.STATUS_FOR_ERROR_TYPE` maps those to **404 / 503 / 504 / 500**. Kept as one explicit table so no failure silently defaults to 500.
- New `wait=true` on `POST /api/patient` runs the job inline (not as a BackgroundTask, which would only execute *after* the response) and returns the data with a real status code. `wait=false` keeps the existing poll-a-jobId flow the Streamlit dashboard uses. If a `wait=true` request dedupes onto an in-flight identical job, it waits on that one instead of starting a second.
- Added `GET /health`, reporting the active job id so a hung instance is visible to a host's probe without reading logs.

**Verified live** against a throwaway server instance: `/health` → 200; patient 1333 → **404** with `errorType: patient_not_found`; patient 1133 → **200** with the plan summary; and with `ATHENA_LOGIN_URL` pointed at a non-resolving host to simulate an outage → **503** with `errorType: site_unavailable`.

## 24. Concurrency: tabs share the login — and the department

**Goal:** handle a few simultaneous requests by running several tabs in one Chromium, since every request authenticates as the same user anyway.

**Finding 1 — `browser.new_page()` does NOT give you a shared session.** It silently creates its own `BrowserContext` with a separate cookie jar, so the "second tab" is logged out (it got bounced to the login page), and Playwright then refuses `context.new_page()` on that implicit context with `Please use browser.new_context()`. Fixed by creating one explicit context in `browser_pool.start()` and adding `new_tab()`; every page must come from it. Confirmed working: a second tab could operate `choosedepartment.esp` without logging in, which is only possible when authenticated.

**Finding 2 — department is SHARED server-side session state, not per-tab.** Measured via the department `<select>` in the `Status` frame (which also revealed departments have internal numeric ids — `4` = SH OH - Shaker, `15` = SH OH - North Canton):

| Step | tab1 | tab2 |
|---|---|---|
| after login | `4` | — |
| tab2 switches to North Canton | `4` | `15` |
| tab1 re-read | **`15`** | `15` |

tab1 changed department **without being touched**. So a department switch in any tab moves *every* tab.

**Design consequence.** Concurrent tabs are only safe for requests sharing one department. A naive page pool would let one request silently pull another request's data from the wrong department — the same class of defect as #22's wrong-patient risk, and just as silent. The correct shape is readers-writer:
- Jobs in the currently active department run concurrently (readers, bounded by pool size).
- A department switch is exclusive (writer): drain all in-flight jobs, switch, then resume.

**Sizing note:** each tab renders the heavy Care Management shadow-DOM pane, so 2-3 tabs want ~4GB (t3.medium), not a t3.small.

## 25. Third search outcome: patient exists under a different provider group

**Found while trying to test concurrency with a second patient id (1134).** The search neither loaded a chart nor showed "does not exist" — it hung until the timeout. What athena actually renders:

```
This enterprise patient has been registered in this provider group, but under a
different patient record.
#1133 - Steadfast Health - OH [22] is the patient record in the current provider group.
Proceed to #1134 - Steadfast Health - TN [21].
```

So a patient search has **three** outcomes, not two: the chart, "does not exist / no permission", and this interstitial. Only the first two were handled, so this one behaved exactly like the bug in #20 — a clear on-screen message ignored in favour of a timeout.

**Fix:** `search_patient` now races all three (`loaded.or_(not_found).or_(other_record)`) and raises `PatientRecordMismatchError`, classified as `patient_record_mismatch` → **HTTP 409**, quoting athena's own text so the caller can see both record ids.

**Deliberately not auto-following the "Proceed to #N" link.** It would return a *different provider group's* record than the caller asked for — the same silent-wrong-data failure mode as #22. Surface it; let the caller choose.

**Incidental but important:** 1133 and 1134 are the **same person** in two provider groups (OH and TN), not two different patients. Worth knowing before treating ids as independent.

## 26. Concurrency: what's proven and what isn't

Prompted by "can two requests work at a time?" — currently **no**, a second concurrent request gets 409 from the single-job lock. On whether it *could*:

**Proven:**
- One login serves multiple tabs, *if* they share a `BrowserContext`. `browser.new_page()` silently creates an isolated context and the resulting tab is logged out (see #24).
- Two tabs concurrently searching the **same** patient both succeeded, both frames ending on the correct `ID=`.
- Department is shared session state across tabs (#24), so cross-department concurrency needs isolation.

**Not proven:** whether two tabs in one context can hold **different** patients simultaneously. The intended test used 1134, which turned out to be the same person's other provider-group record (#25), so it never tested what it was meant to.

**The user's own suggestion is the better architecture, and it is now PROVEN.** One Chromium process, one `BrowserContext` per concurrent slot, each with its own login and department. Test: ctx A logged into SH OH - Shaker on patient 1133, ctx B logged into SH TN - Patterson on patient 1134 (the same person's two provider-group records), both live at once:

| | department | patient |
|---|---|---|
| ctx A | `4` (Shaker) | 1133 |
| ctx B | `3` (Patterson) | 1134 |

A kept both its department and its patient while B switched departments and loaded a different record — the exact scenario that broke with tabs in a shared context. Contexts isolate properly.

Two secondary worries also cleared by the same run:
- **Back-to-back logins on one account worked.** The rapid-login throttling of #8 did not trigger.
- **TOTP staggering is cheap** — waiting for a fresh 30s window cost ~6s in practice. Codes are single-use per window, so logins must still be staggered, but it is not a meaningful constraint.

**Resulting design:** N contexts (N = desired concurrency, e.g. 2-3), created lazily, each holding its own login + department. Route a request to a context already in the right department if one is free; otherwise take a free context and switch it; otherwise queue. The single-job lock becomes a semaphore of N. Sizing is driven by concurrent *tabs* rendering the heavy pane — budget ~4GB (t3.medium) for 2-3.

**Still unknown (and now moot):** whether two tabs *within one context* can hold different patients. The per-context design sidesteps the question entirely.

## 1. Playwright: `chrome.exe` failed to launch — "side-by-side configuration is incorrect"

**Symptom:** `BrowserType.launch` failed with a Windows SxS (side-by-side assembly) error, both from Playwright and when running `chrome.exe --version` directly.

**Root cause:** Not a missing VC++ Redistributable (both x64 and x86 were already installed — confirmed via `winget`). Not a corrupted download either (reproduced after a clean reinstall via PowerShell). It was simply a bad/incompatible Chromium build (`1134`, pulled by the originally-pinned `playwright==1.47.0`) for this specific Windows machine. `sxstrace` couldn't get a definitive root cause without admin elevation, so we didn't chase it further.

**Fix:** Upgraded `playwright` to `1.62.0` in `requirements.txt`, which pulls a newer Chromium build (`1234`). That build launches cleanly. Confirmed with a full `browser.launch()` → `goto()` → `title()` round trip.

## 2. Streamlit: `TypeError` on `st.image(..., width="stretch")`

**Symptom:** `TypeError: '<=' not supported between instances of 'str' and 'int'` inside `st.image`.

**Root cause:** `width="stretch"` is a newer Streamlit API (string literal support for `width`). The installed version here is `1.38.0`, which only accepts `width: int | None`.

**Fix:** First tried `use_container_width=True` — also not supported in 1.38.0 (`st.image` in that version doesn't have that kwarg either). Checked the actual installed signature via `inspect.signature(st.image)` and used what it actually supports: `use_column_width=True`.

**Lesson:** When an API call fails with a surprising type error against a third-party library, check the *installed* version's real signature instead of assuming the latest docs apply.

## 3. Login "succeeded" per step log, but timed out waiting for `#search`

**Symptom:** All login + MFA steps logged success, but the final `wait_for_selector("#search")` timed out. Log showed navigation had actually reached `choosedepartment.esp`.

**Root cause:** Not a login failure — login + TOTP genuinely worked. We just didn't know about the department-selection step that sits between MFA and the real app shell for multi-department sandbox accounts. `#search` only exists after a department is chosen.

**Fix:** Added a department-selection step to `login.py` (fill `#DEPARTMENTID` if needed, click `#loginbutton`), and changed the "login confirmed" check to a URL pattern (`preview.athenahealth.com/**`) right after MFA, with a further wait for the department page's own submit button.

## 4. Step-log screenshots didn't visually match their message

**Symptom:** User watched the live view and saw the TOTP code apparently never get typed into the MFA field, even though the step log said "Generated TOTP code, filling it in" — but the flow actually succeeded.

**Root cause:** In `login.py`, `on_step(message)` (which captures a screenshot) was called **before** the corresponding `page.fill`/`page.click`. So each screenshot showed the state *before* that step's action, one step behind what the message described.

**Fix:** Reordered every step so the action runs first, then `on_step` logs + screenshots the result. Now the live view accurately reflects "what just happened."

## 5. Department "Go" button click did nothing (visually)

**Symptom:** Step log said "Submitted department selection", but the live-view screenshot still showed the department page, and the next `wait_for_selector("#searchinput")` timed out.

**Root cause:** Two things layered together — (a) the screenshot timing bug from #4 made it look like nothing happened even when it had, and (b) `#searchinput` genuinely isn't reachable the way we were checking for it (see #6). A standalone debug script proved the `#loginbutton` click **did** navigate correctly (`choosedepartment.esp` → `globalframeset.esp?MAIN=...`).

**Fix:** No change needed to the click itself — fixed by #4 (ordering) and #6 (frame targeting) together.

## 6. `#searchinput` / `#search` never found by `page.wait_for_selector`

**Symptom:** Timeout waiting for `#searchinput` even though it's visibly on the page (confirmed via screenshot).

**Root cause:** athenahealth's app shell is a classic multi-frame layout (`globalframeset.esp`). The search box lives inside a child `<iframe id="GlobalNav">`, not the top-level document. Playwright's `page.wait_for_selector()` / `page.locator()` only search the main frame by default — they don't pierce into iframes.

**Fix:** Switched every search-box interaction to go through `page.frame_locator("iframe#GlobalNav")` instead of plain `page.locator(...)`. Confirmed the full frame tree via a debug script that lists `page.frames` (11+ nested frames in this app — `GlobalNav`, `GlobalWrapper`, `Status`, `frameContent`, `frMain`, etc.).

## 7. "Patient ID" dropdown option never appeared after typing an ID

**Symptom:** After `page.fill()`'ing the patient ID into the search box (inside the `GlobalNav` frame), the "Patient ID" dropdown option never rendered — `get_by_text("Patient ID")` timed out. Searching every frame on the page for that text also came up empty.

**Root cause (two layered issues):**
- `page.fill()` sets the input's value directly without dispatching real per-character keystroke events. This search box's dropdown is wired to `onkeyup="SearchKeyUp(...)"`, so a bulk value-set never triggers it.
- Even after switching to `press_sequentially()` (real keystroke-by-keystroke typing), the dropdown *still* didn't open. Screen recording from the user showed the actual required UX: type the ID (dropdown does **not** open yet), then **click the field again** (with the value already in it) — only then does the dropdown render.

**Fix:** Automation sequence is now: click the field → type the ID via `press_sequentially` → click the field again → wait for the "Patient ID" option to appear → click it. (In progress — see current work.)

## 9a. "Could not find frame named 'frMain'" — intermittent, 3 times in a row

**Symptom:** After several runs completed fine, `search_patient()` and later `open_care_management_pane()` started failing consistently with `RuntimeError: Could not find frame named 'frMain'`, right after previously-working steps.

**Initial (wrong) theory:** Suspected external flakiness — Okta/athenahealth backend latency, or even a concurrent login to the same sandbox account invalidating the session. Flagged this to the user as a possible root cause and asked them to avoid concurrent logins.

**Actual root cause:** A real bug in our own code. Both `patient_search.py` and `care_plan.py` called `page.frame(name="frMain")` as an **instant, one-shot check** — not a wait — immediately after triggering a navigation (pressing Enter to submit the search; clicking Quickview). `page.frame()` returns whatever the frame tree looks like at that exact instant; if the navigation involves the iframe briefly detaching and a new one reattaching (rather than a clean same-frame navigation), checking in that split-second gap returns `None` even though the frame reappears moments later. This explains why it "usually worked" — most of the time the check happened to land after the frame settled — and why it started failing repeatedly once network conditions were slightly slower.

**Fix:** Added `_wait_for_frame(page, name, timeout_ms)` in `patient_search.py` — polls `page.frame(name=...)` every 200ms up to a timeout instead of checking once. Used it at both call sites (`patient_search.py` and `care_plan.py`) instead of the instant check.

**Lesson:** `page.frame(name=...)` is a snapshot, not a wait — never call it immediately after triggering a navigation without polling/retrying. Don't assume "intermittent failure right after a network-bound action" is external flakiness before checking whether the check itself is racy.

**Confirmed fixed:** full run succeeded end-to-end after the fix, including all of Treatment Plan Summary, Concerns, Behavioral Health Goals (baseline description + treatment modalities, verified against real data), and Client Characteristics (all 5 groups, verified against the user's original HTML dump).

## 9. Navigating to the Care Management / Treatment Plan pane

Three separate issues stacked on top of each other here, each fixed one at a time:

- **Frame scoping (again):** `page.click()`/`page.wait_for_selector()` on the Quickview button and everything after it silently failed (element not found) because the whole patient chart UI — Quickview button included — lives inside the `frMain` frame, not the top-level page. Same root cause as TROUBLESHOOTING.md #6. Fixed by getting `page.frame(name="frMain")` and calling everything through that Frame object instead.

- **`load` event never fires:** wrapping the Quickview click in `frame.expect_navigation()` with the default `wait_until="load"` hung indefinitely — the log showed `"domcontentloaded" event fired` but never `load`. This legacy page apparently keeps some connection open (polling/long-lived request) that prevents the browser's `load` event from ever firing cleanly. Fixed by passing `wait_until="domcontentloaded"` explicitly, which is enough to safely interact with the page.

- **Sidebar icon looked right but wasn't clickable:** we initially tried clicking a specific chart-nav sidebar icon by position (`li:nth-child(11)`), confirmed via Playwright's own error log to correctly resolve to `<span class="nimbus-icon-care-plan...">` — genuinely the right icon — but it stayed "not visible" through 30s of retries (icon-font glyphs can have zero rendered box size, and it may already be the active/selected tab by default). **Fixed by not using it at all**: a screenshot at the failure point showed the page already has a direct **"Go to Treatment Plan »"** text link sitting in the Care Management widget — clicking that by text is simpler and more robust than fighting the icon.

- **Pane loads slowly:** even after the link click succeeds, the resulting pane (a heavy shadow-DOM React widget) shows a loading spinner for well over 15 seconds before content renders. Bumped that specific wait to 45s.

**Result:** Concerns and top-level Behavioral Health Goal fields (status, priority, title, statement, targeted concerns, dates, attribution) all extract correctly, verified against real patient data. Objectives/Interventions extraction is still a known gap — the "click every Show More button" step doesn't reliably expand every goal (clicking one likely shifts layout and breaks locators queued for the others, silently swallowed by a broad `except Exception: pass`). Next step: expand and extract one goal at a time instead of collecting all "Show More" locators up front.

**Side note:** The dropdown itself renders inside yet another frame, `searchmenuiframe`, which Playwright reports as `about:blank` because its content is written via JS rather than a real page navigation — worth remembering if a future selector search inside it comes up empty by URL/frame-list assumptions alone.

**Resolution (final):** The dropdown is NOT in a separate frame after all — that theory was wrong. Proved via three escalating checks, all on the main frame:
- Plain `document.querySelectorAll('body *')` filtering for exact text "Patient ID": zero matches.
- Same query recursing into every element's `.shadowRoot` too (in case of a closed/open shadow DOM widget): still zero matches.
- Playwright's `aria_snapshot()` (the accessible/rendered tree, same thing a screen reader sees): the *entire* dropdown showed up as **one single flattened text node** — `"...CLAIMS Claim ID CLINICAL ITEMS Document ID PATIENTS Patient ID Date of birth..."` — not as separate labeled rows.

That last result explains everything: this is a legacy custom-built dropdown with **no per-row DOM elements at all**. The whole menu is one text blob, and the site's own click handling must work by coordinate math (which line was clicked), not by binding a handler to a "Patient ID" element — because no such element exists to bind to.

**Fix:** Stopped looking for a DOM element entirely. Used the browser's own `window.find()` API (the same thing Ctrl+F uses) to locate exactly where the substring "Patient ID" renders on screen — `window.find(text, ...)` + `window.getSelection().getRangeAt(0).getBoundingClientRect()` — then clicked that literal pixel coordinate via `page.mouse.click(x, y)`. This correctly selected the category (confirmed visually: the search box's icon changed to the patient-silhouette icon). Selecting the category alone doesn't submit the search — needed an explicit `Enter` on the search box afterward, which navigated `frMain` (the frame the patient page loads into) to `client/clientsummary.esp?ID=<patient_id>`. Full flow confirmed working end-to-end.

## 10. Objectives/Interventions extraction returned empty despite real populated data

**Symptom:** User provided a screenshot proving a goal ("Reduce opioid use from 3 times weekly to 1 time on weekends") had real, populated Objectives/Interventions in the live UI, but our extraction returned empty lists for it every time.

**Root cause (three layered bugs):**
- **XPath too loose:** `_extract_task_card`'s ancestor lookup used `contains(@class, 'acc_c_careplan-container-card')`. BEM-style child classes like `acc_c_careplan-container-card__header-col` also contain that substring, so the query matched a shallow child span instead of the real outer card div several levels up — meaning we were reading from the wrong element entirely.
- **Timing:** selecting a goal re-fetches its Objectives/Interventions/Progress asynchronously. The existing wait looked for a `.block-ui.fe_is-loading` spinner to appear then clear — confirmed via an instrumented test that this spinner condition never actually triggers (silently burning the full timeout on every goal, for nothing), and that reading immediately after "clearing" was still too early: content was reliably absent at +0s but fully present by +3s in repeated tests.
- **Wrong title selector:** item title extraction used the first line of the item's inner text, which is actually the item's category label ("Behavioral Objective"/"Behavioral Intervention"), not the real title. The real title lives in `.acc_c_task-panel-twopane_view__task-title-row h1`.

**Fix:** Exact-class-token XPath match (`contains(concat(' ', normalize-space(@class), ' '), ' acc_c_careplan-container-card ')`); replaced the spinner-wait with a flat `page.wait_for_timeout(4000)`; retargeted title extraction to the real `h1` selector. All three in `_extract_task_card` / `_extract_expanded_goal_details` in `automation/care_plan.py`.

**Confirmed fixed:** Full run against patient 1133 — every real goal now shows correct non-zero objective/intervention counts, with real titles like "Develop coping strategies for chronic pain, opioid cravings, and triggers during weekly individual counseling" instead of the generic "Behavioral Objective" label.

**Lesson:** A substring-based `contains(@class, ...)` XPath match is unsafe with BEM-style class naming — always pad with spaces and match the exact token. Also: a loading-state class that "never triggers" is a sign to measure real timing directly rather than trusting the app's own loading indicator.

## 11. Goal Progress History — resolved with user-supplied HTML, no longer a guess

**Symptom:** `_extract_goal_progress_history` had never been verified against real populated data — no test patient had progress entries logged, so it was pure best-effort guessed selectors.

**Fix:** User located a goal with real progress history in the sandbox UI and supplied a screenshot plus the real outerHTML for both the summary view and the expanded history list. This showed:
- The card only displays the *latest* entry until a "Show Progress History (N items)" toggle is clicked, which expands `.acc_c_goal-progress-status-list__progress-status-row` rows.
- Each row has a date + status tag in `.acc_c_goal-progress-status-list__status-history-title` (nested spans — flattened by `inner_text()`), an optional `.acc_c_goal-progress-status-list__status-history-reason`, and the same `.acc_c_cm-attribution-label` attribution pattern used everywhere else on this page.

Rewrote `_extract_goal_progress_history` in `automation/care_plan.py` to click the toggle first (only if still showing "Show", to avoid double-toggling an already-expanded state), then parse every row for date/status/reason/attribution — reading the full history rather than just the current status.

**Confirmed fixed:** Full run against patient 1133 — the "Reduce opioid use..." goal now correctly returns both real entries ("Achieved" and "No Change", both dated 09-05-2026), matching the user's screenshot exactly.

**Lesson:** When a field can't be verified because no test data exists, the fastest unblock is asking the person with sandbox access to add/find real data and hand over the outerHTML directly — much faster than guessing selectors from a screenshot alone.

## 8. Repeated rapid logins started timing out at the MFA step

**Symptom:** After many back-to-back debug runs (each doing a full login) in a short window (~15 minutes), two consecutive runs hung at "Submitted MFA form" waiting for the post-MFA redirect, where every earlier run had succeeded quickly.

**Likely cause:** Not investigated to a root cause yet — most likely explanation is Okta/athenahealth-side throttling or a soft flag on the account from repeated automated logins in a short period (this app's identity provider is Okta, per the `identity_okta_login_frame_...` frame seen in the frame list). Could also just be coincidental TOTP-window timing flakiness happening twice in a row.

**Action taken:** Paused automated retries rather than hammering it further, to avoid worsening a possible real lockout. Plan: wait a few minutes, then retry once to confirm normal login still works before resuming other debugging. **If this recurs, pace debug runs further apart rather than back-to-back.**
