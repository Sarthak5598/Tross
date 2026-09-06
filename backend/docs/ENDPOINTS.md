# Upstream endpoints

Every piece of data this service returns comes from athenahealth's
**Care Management GraphQL API** — the same API the Treatment Plan UI
drives itself. We call it directly instead of scraping the rendered page.

```
POST https://caremanagement.preview.api.athena.io/caremanagement-api/graphql
```

Operation documents live in `automation/gql_queries.py`, transcribed
verbatim from the app's own DevTools captures rather than reconstructed
from a schema — so they stay valid against whatever the server actually
accepts.

> **This is an internal, undocumented API.** athenahealth may change it
> without notice. The mitigation is that a shape change here fails loudly
> (`GraphQLError`) rather than silently returning an empty list, which is
> exactly how three DOM-path regressions previously went unnoticed.

## Auth

Four headers, all captured from a live logged-in browser session — see
`automation/token_source.py`. Every captured header is replayed verbatim,
not a curated subset.

| Header | Notes |
|---|---|
| `authorization` | `Bearer <jwt>`, **~5 minute lifetime** |
| `x-athena-context` | practice id |
| `x-athena-environment` | e.g. `preview@nva`. Easy to miss and **not optional** — without it the request authenticates and then fails inside the resolver with "Unspecified Athena environment" |
| `x-athena-department` | numeric id. Verified **not** to affect care-plan responses (identical bytes across three departments), but sent for fidelity |

## Operations

Five operations. Only the first is sequential; everything else fans out
in parallel once it returns.

| # | Operation | Called | Gives us |
|---|---|---|---|
| 1 | `GetPatientCarePlanInternal` | once | The spine. All health concerns, their goals, objectives, interventions, baseline, modalities, review dates. Everything below needs the concern/goal ids it returns. |
| 2 | `GetTaskSchedulesWithScheduledTasks` | 1 per concern | Task schedules and scheduled tasks. `goalId` is nullable — omitting it returns every goal's schedules in one call. **The slowest single call (~9s)** and therefore the current latency floor. |
| 3 | `GetGoalStatusHistoryInternal` | 1 per **goal** | Goal progress history. Cannot be batched — one call each, so this is the bulk of the fan-out (32 calls for patient 1133). **Off by default** — opt in with `include_history=true`. |
| 4 | `GetAllHealthConcernAttestationsInternal` | 1 per concern | Attestations. |
| 5 | `GetObservations` | once | Observations, mapped to concerns/characteristics. Note `HealthConcernId` on an observation is really the **Problem** id. |

## Call graph

```
GetPatientCarePlanInternal          ~2.9s   must be first: yields the ids
        |
        +-- one parallel fan-out, 16 at a time ------------- ~9s
              GetTaskSchedulesWithScheduledTasks   x concerns
              GetAllHealthConcernAttestationsInternal x concerns
              GetGoalStatusHistoryInternal         x goals   (only if include_history)
              GetObservations                      x1
```

2 and 3 depend only on 1, **not on each other** — an earlier version
awaited them in separate phases and serialised ~5.7s behind ~9.1s for no
reason. Merging them into one fan-out took a full request 18.2s -> 12.2s.

## Concurrency

Measured, 34-call fan-out, 3 runs each, `_probe.py`:

| workers | wall | median per call |
|---|---|---|
| 4 | 19.4s | 1.98s |
| 8 | 11.0s | 2.04s |
| **16** | **9.2s** | 2.06s |
| 32 | 9.3s | 5.05s |

**We are not being throttled** — zero non-200s across every run, and
median per-call latency is flat from 4 to 16. Wall clock stops improving
at 16 because it has reached the slowest single call (~9s); the rise at
32 is our own thread-pool queueing, not their server. Tunable via
`GQL_MAX_CONCURRENCY`.

## Everything is dynamic

No pagination arguments, no result caps, no `[0]` indexing into a
collection. All list handling iterates:

- **concerns** — `build_result()` walks every entry in `HealthConcerns`.
  An earlier version read `HealthConcerns[0]` and looked correct only
  because the test patient had one concern; it silently lost data for
  patient 1133, which has two.
- **goals / objectives / interventions** — iterated per concern.
- **"show more" in the UI is not a factor here.** That's DOM-level
  paging; the GraphQL response carries the complete array in one
  payload. Being immune to it is a large part of why this path replaced
  the scraper.

Verified against patients with up to 2 concerns / 32 goals. Nothing in
the code is bounded by those numbers, but no larger record has been
exercised yet.

## Departments

Eight, exposed as a stable enum (`automation/departments.py`) and
discoverable at `GET /api/departments`:

| code | label | athena id |
|---|---|---|
| `SH_TN_PATTERSON` | SH TN - Patterson | 3 |
| `SH_OH_SHAKER` | SH OH - Shaker | 4 |
| `SH_OH_NORTH_CANTON` | SH OH - North Canton | 15 |
| `SH_OH_WEST_CLEVELAND` | SH OH - West Cleveland | 14 |
| `IPC_TN_WEST` | IPC TN - Ascension St. Thomas West | 5 |
| `IPC_TN_MIDTOWN` | IPC TN - Ascension St. Thomas Midtown | 12 |
| `IPC_TN_RIVER_PARK` | IPC TN - Ascension St. Thomas River Park | 16 |
| `IPC_TN_CENTENNIAL` | IPC TN - HCA Centennial | 13 |

Callers send the **code**. Display labels are still accepted for
backwards compatibility, but a label is athenahealth's to rename and the
code is ours.

Department rides in a request header, so one token serves all eight and
two departments can be queried concurrently.

## Error contract

| condition | status | errorType |
|---|---|---|
| ok | 200 | — |
| unknown department code, non-numeric patient id | 400 | `invalid_request` |
| patient id doesn't resolve | 404 | `patient_not_found` |
| id belongs to another provider group | 409 | `patient_record_mismatch` |
| duplicate concurrent DOM job | 409 | — |
| athenahealth unreachable | 503 | `site_unavailable` |
| exceeded `JOB_TIMEOUT_S` | 504 | `timeout` |
| our bug | 500 | `automation_error` |

Two upstream quirks worth knowing:

- **athenahealth reports a missing patient as HTTP 200**, with the real
  status buried in the GraphQL `errors` array. It has to be dug out, or a
  bad id surfaces as our 500 and reads as "this service is broken".
  There are **two wordings**, and matching only the first missed the
  second entirely:
  - unknown id -> `CODE: 404` / "The Patient ID or Department ID is invalid."
  - id outside the active department -> no code at all, just "The
    specified patient does not exist in that department."

  Both now map to 404; `must be integer` maps to 400.
- **Those error payloads embed a full Node stacktrace.** We extract the
  one useful line (`MDP_ERROR`) and discard the rest — returning it raw
  leaked internals and buried the actual cause. A 404 body went from
  600+ bytes of stacktrace to 165 bytes.

## Session recovery

The browser is not in the request path, but it is still the only source of
tokens — so a wedged session is a total outage. Two things guard it:

- `is_logged_in()` checks the search box is actually *visible*, not just
  that the app shell's JS loaded. The JS check alone passes on a shell
  whose nav never rendered.
- That check is still not sufficient — a page can pass it and then fail on
  the search *dropdown*. So token acquisition gets **two attempts**, and
  a failure between them throws the page away (`browser_pool.reset_page`)
  and forces a real re-login rather than retrying into the same wedge.

Without the second part this failed for real: `hasHeaders: true`,
`hasToken: false`, and `"Could not locate 'Patient ID' option in search
dropdown"` pinned in `/health` while the renewal loop retried the same
broken page every 30s indefinitely. Covered by
`tests/test_session_recovery.py`.

## Which plans are returned

A patient's `HealthConcerns` are not all in scope, and merging them all
was producing wrong output.

| kind | `HealthConcernType` | default |
|---|---|---|
| Treatment Plan, live | `Behavioral`, `IsArchived: false` | **returned** |
| Treatment Plan, archived | `Behavioral`, `IsArchived: true` | excluded — `include_archived=true` |
| Care Plan | `Longitudinal` | excluded — `include_care_plan=true` |

`HealthConcernType` is athenahealth's own discriminator. Matching the
display name ("Treatment Plan created on 06-25-2026") would work today
but it is user-facing text and free to change.

**Archived plans were the real bug.** Patients 1135 and 1136 each carry
three Treatment Plans, two archived. Merging them reported **14 goals
where the live plan has 5** — superseded goals indistinguishable from
current ones. Filtering happens *before* the fan-out, so archived goals
don't cost a progress-history call either.

Nothing is hidden silently. Every response carries `planScope`:

```json
"planScope": {"returned": 1, "totalOnRecord": 3,
              "excludedCarePlan": 0, "excludedArchived": 2,
              "includeCarePlan": false, "includeArchived": false}
```

so a caller can tell "this patient has no archived plans" from "we hid
them", and each row is tagged `plan_type` and `is_archived`.

Measured effect:

| patient | default | `include_archived=true` |
|---|---|---|
| 1133 | 32 goals, 1 plan of 2 | 32 goals |
| 1135 | **5 goals**, 1 plan of 3 | 14 goals, 3 plans |
| 1136 | **5 goals**, 1 plan of 3 | 14 goals, 3 plans |

## Two fields named almost the same. Only one is the plan discriminator.

| field | on | meaning |
|---|---|---|
| `HealthConcernType` | a health concern | **The discriminator.** `Behavioral` = Treatment Plan, `Longitudinal` = Care Plan. |
| `HealthConcernTypes` | `AssignedTask` | **NOT the discriminator.** A property of the task template. |

Verified on patient 1133: all **57** task schedules under concern 1483 —
which is `Behavioral`, i.e. the Treatment Plan — carry
`AssignedTask.HealthConcernTypes: ["Longitudinal"]`, while every goal they
attach to belongs to that Behavioral concern.

```
AssignedTask.HealthConcernTypes   : ['Longitudinal']   <- says Care Plan
parent concern type of their goals: ['Behavioral']     <- is Treatment Plan
```

So **filtering tasks on `HealthConcernTypes` would drop every objective and
intervention** while looking like a sensible consistency fix. Filter plans
at the concern level only. Nothing in the code does otherwise today; this
note exists so it stays that way.

`AssignedTask.PatientAssignable` is the other load-bearing field on this
call — it is what splits objectives (`true`) from interventions (`false`),
30/27 on patient 1133. A trimmed projection that omits it breaks that
split silently rather than erroring.
