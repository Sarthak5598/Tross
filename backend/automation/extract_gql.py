"""Maps Care Management GraphQL responses onto the JSON shape the API
already returns, so switching data sources is invisible to callers.

Every mapping here is checked against DOM-extracted output for patient
1133 (the counts recorded in TROUBLESHOOTING.md #10). Where a mapping is
*not* verified, it says so at the point of the mapping rather than
looking equally confident as the rest — that distinction is the whole
lesson of this project, where three separate bugs hid behind results
that looked structurally fine.
"""

# NOTHING IS FILTERED OUT. Every goal and concern the API returns is
# returned to the caller, each carrying its own status.
#
# This is deliberate, and it is the safer choice for medical data. We know
# which values the UI displays — goals with LifecycleStatus "active" (6 of
# 32 for patient 1133) and problems with ConcernStatus "NotAchieved"
# (14 of 32) — and filtering on those reproduces the screen exactly.
#
# But `LifecycleStatus` and `ConcernStatus` are plain Strings in the
# schema, NOT enums, so athenahealth publishes no list of valid values. A
# status we have never observed (say "completed") would be silently
# dropped by such a filter, and silently dropping a patient's clinical
# record is a far worse failure than returning extra rows a caller can
# ignore. It is also unverifiable: "did we guess the filter right?" cannot
# be answered, whereas "did we return what the API gave us?" can.
#
# Callers wanting to mirror the UI filter on the status fields below;
# an agent asking "has this patient previously achieved sobriety goals?"
# needs the rows a UI-parity filter would have discarded.
UI_VISIBLE_LIFECYCLE = "active"        # for reference, not applied
UI_VISIBLE_CONCERN_STATUS = "notachieved"  # for reference, not applied

# GetObservations groups characteristics by TypeName. Four map directly;
# "Barrier" -> "needs" is INFERRED from the UI's labelling and is the one
# pairing here that has not been checked value-by-value against the DOM.
# Department name -> the numeric id the x-athena-department header wants.
DEPARTMENT_IDS = {
    "IPC TN - Ascension St. Thomas Midtown": "12",
    "IPC TN - Ascension St. Thomas River Park": "16",
    "IPC TN - Ascension St. Thomas West": "5",
    "IPC TN - HCA Centennial": "13",
    "SH OH - North Canton": "15",
    "SH OH - Shaker": "4",
    "SH OH - West Cleveland": "14",
    "SH TN - Patterson": "3",
}

# Objectives vs interventions. The DOM read these from two separate cards;
# the API returns them as one list, split by whether the task is assigned
# to the patient. Verified on goal 1942: 3 PatientAssignable=True against
# the DOM's 3 objectives, 3 False against its 3 interventions. It also
# reads correctly — interventions are staff tasks, and their names carry
# role prefixes ("Therapist:", "Care team:").
OBSERVATION_TYPE_TO_SECTION = {
    "Strength": "strengths",
    "Barrier": "needs",       # UNVERIFIED — see above
    "Ability": "abilities",
    "Preference": "preferences",
    "Support": "supports",
}


def _actor_name(actor: dict | None) -> str | None:
    if not actor:
        return None
    name = actor.get("Name") or {}
    parts = [name.get("FirstName"), name.get("LastName")]
    joined = " ".join(p for p in parts if p)
    return joined or actor.get("Id")


def _attribution(node: dict | None) -> dict:
    """The DOM path surfaced 'added_by'/'last_action_by' as one string
    combining actor and timestamp. Kept identical so callers don't have to
    change, even though the API gives them separately."""
    attribution = (node or {}).get("Attribution") or {}
    who = _actor_name(attribution.get("Actor"))
    when = attribution.get("DateTime")
    combined = f"{who} | {when}" if who and when else (who or None)
    return {"added_by": (node or {}).get("AddedBy") or who,
            "last_action_by": combined}


def map_goals(concern: dict, schedules: list[dict]) -> list[dict]:
    """Goals for one health concern, with their objectives/interventions.

    `schedules` is the flat TaskSchedules list from
    GetTaskSchedulesWithScheduledTasks called WITHOUT a goalId — one
    request covering every goal — matched back to goals via GoalIds.
    """
    by_goal: dict[str, list[dict]] = {}
    for entry in schedules:
        task_schedule = entry.get("TaskSchedule") or {}
        if task_schedule.get("IsDeactivated"):
            continue
        for goal_id in (task_schedule.get("GoalIds") or []):
            by_goal.setdefault(str(goal_id), []).append(task_schedule)

    goals = []
    for goal in (concern.get("Goals") or []):
        objectives, interventions = [], []
        for task_schedule in by_goal.get(str(goal.get("Id")), []):
            assigned = task_schedule.get("AssignedTask") or {}
            period = ((task_schedule.get("TimePeriod") or {}).get("Requested")) or {}
            item = {
                "title": assigned.get("Name"),
                "Start Date": period.get("StartDate"),
                "Target Date": period.get("EndDate"),
                "task_status": task_schedule.get("TaskStatus"),
                **_attribution(task_schedule),
            }
            (objectives if assigned.get("PatientAssignable")
             else interventions).append(item)

        goals.append({
            "goal_id": goal.get("Id"),
            "status": goal.get("AchievementStatus"),
            # Both statuses are surfaced so a caller can reproduce the UI
            # view (lifecycle_status == "active") or reason over the full
            # history. shown_in_ui flags UI parity without enforcing it.
            "lifecycle_status": goal.get("LifecycleStatus"),
            "shown_in_ui": (goal.get("LifecycleStatus") or "").lower() == UI_VISIBLE_LIFECYCLE,
            "priority": goal.get("Priority"),
            "title": goal.get("Name"),
            "client_statement": goal.get("PatientStatement"),
            "start_date": goal.get("StartDate"),
            "review_date": goal.get("ReviewDueDate"),
            "target_date": goal.get("EndDate"),
            "baseline_description": goal.get("BaselineDescription"),
            # The DOM rendered these pipe-joined; preserved for parity.
            "treatment_modalities": "|".join(
                m.get("Name") for m in (goal.get("Modalities") or []) if m.get("Name")
            ) or None,
            "goal_progress_history": [],  # filled by map_progress_history
            "objectives": objectives,
            "interventions": interventions,
            **_attribution(goal),
        })
    return goals


def map_progress_history(history: dict) -> list[dict]:
    """AchievementStatusHistory -> the DOM's goal_progress_history shape."""
    entries = (history.get("getGoalStatusHistoryInternal") or {}).get(
        "AchievementStatusHistory") or []
    return [{
        "date": entry.get("DateTimeAdded"),
        "status": entry.get("Status"),
        "reason": entry.get("StatusReason"),
        "last_action_by": _actor_name(entry.get("Actor")),
    } for entry in entries]


def _observations_by_problem(observations: dict) -> dict:
    """Index observations by the problem they belong to.

    The link field is named `HealthConcernId` but actually holds the
    **Problem** id — confirmed by all 32 problem ids matching exactly. The
    misleading name is why `evidenced_by` looked unavailable at first.
    """
    index: dict[str, list[dict]] = {}
    for observation in (observations or {}).get("getObservations") or []:
        for link in (observation.get("HealthConcerns") or []):
            index.setdefault(str(link.get("HealthConcernId")), []).append(observation)
    return index


def map_concerns(concern: dict, observations: dict | None = None) -> list[dict]:
    """Problems -> the DOM's `concerns` list, with ICD descriptions.

    `evidenced_by` comes from the problem's Symptom observation (e.g.
    "Active diagnosis on the patient's problem list as of 2026-09-04"),
    NOT from the problem itself — Problem.Note and Problem.Symptom are
    null for every record we have seen.
    """
    by_problem = _observations_by_problem(observations or {})
    concerns = []
    for problem in (concern.get("Problems") or []):
        diagnoses = []
        for diagnosis in (problem.get("Diagnoses") or []):
            icd = diagnosis.get("ICDData") or {}
            diagnoses.append(icd.get("Description")
                             or diagnosis.get("DiagnosisCode"))
        linked = by_problem.get(str(problem.get("Id")), [])
        symptoms = [o.get("Summary") for o in linked
                    if o.get("TypeName") == "Symptom" and o.get("Summary")]
        concerns.append({
            "title": problem.get("Description"),
            "evidenced_by": (problem.get("Note") or problem.get("Symptom")
                             or (symptoms[0] if symptoms else None)),
            # Every linked symptom, not just the one used for display —
            # `evidenced_by` picks a single value for the summary line and
            # would otherwise be the only place these surfaced.
            "symptoms": symptoms,
            "associated_diagnoses": [d for d in diagnoses if d],
            "status": problem.get("ConcernStatus"),
            "shown_in_ui": (problem.get("ConcernStatus") or "").lower() == UI_VISIBLE_CONCERN_STATUS,
            "start_date": problem.get("StartDate"),
            **_attribution(problem),
        })
    return concerns


def map_client_characteristics(observations: dict) -> dict:
    """Observations grouped by TypeName.

    Note the API also returns Symptom and "Baseline Description" types,
    which the Client Characteristics panel does not show — they are
    deliberately dropped rather than invented into a section.
    """
    result = {section: [] for section in OBSERVATION_TYPE_TO_SECTION.values()}
    # Types the Client Characteristics panel doesn't show (Symptom,
    # "Baseline Description") still come back, under their own key, rather
    # than being discarded — same reasoning as the goal/concern statuses.
    result["other"] = []
    for observation in (observations.get("getObservations") or []):
        section = OBSERVATION_TYPE_TO_SECTION.get(observation.get("TypeName"), "other")
        result[section].append({
            "title": observation.get("Summary"),
            "type": observation.get("TypeName"),
            "archived": bool(observation.get("Archived")),
            "added_by": _actor_name(observation.get("CreatedBy")),
        })
    return result


def map_attestations(attestations: dict) -> list[dict]:
    """Verified against a real attestation added to patient 1133."""
    items = attestations.get("getAllHealthConcernAttestationsInternal") or []
    return [{
        "id": item.get("Id"),
        "status": item.get("Status"),
        "plan_name": item.get("PlanName"),
        "pdf_link": item.get("PdfLink"),
        "patient_attested_status": item.get("PatientAttestedStatus"),
        "patient_attested_date": item.get("PatientAttestedDate"),
        "provider_attestations": [{
            "provider": p.get("ProviderDisplayName"),
            "status": p.get("ProviderAttestedStatus"),
            "note": p.get("ProviderAttestedNote"),
            "date": p.get("ProviderAttestedDate"),
        } for p in (item.get("ProviderAttestations") or [])],
    } for item in items]


def map_plan_summary(concern: dict) -> dict:
    return {
        "review_date": concern.get("ReviewDueDate"),
        "plan_added_by": concern.get("AddedBy"),
        **{k: v for k, v in _attribution(concern).items()
           if k == "last_action_by"},
    }


# athenahealth's own discriminator between the two plan kinds. Far more
# reliable than matching the display name ("Treatment Plan created on
# 06-25-2026"), which is user-facing text and free to change.
TREATMENT_PLAN_TYPE = "Behavioral"
CARE_PLAN_TYPE = "Longitudinal"
PLAN_TYPE_LABELS = {TREATMENT_PLAN_TYPE: "Treatment Plan",
                    CARE_PLAN_TYPE: "Care Plan"}


def select_concerns(concerns: list[dict], include_care_plan: bool,
                    include_archived: bool) -> list[dict]:
    """Which health concerns belong in the response.

    Two exclusions, both default-on, and both about not presenting stale
    or out-of-scope records as if they were current:

    * **Care Plan** (`Longitudinal`) — out of scope for this service,
      which is about the Treatment Plan. On patient 1133 it is empty and
      harmless, but it still produced a second, meaningless planSummary
      row.

    * **Archived plans** (`IsArchived`) — this one actually corrupted
      output. Patients 1135/1136 each carry THREE treatment plans, two
      archived, and merging them reported 14 goals where the live plan has
      5. Superseded goals were indistinguishable from current ones.

    Both are recoverable via flags rather than dropped outright, and every
    returned row is tagged with `plan_type` / `is_archived`, so nothing is
    hidden — it just isn't silently blended together.
    """
    kept = []
    for concern in concerns:
        if concern.get("HealthConcernType") == CARE_PLAN_TYPE and not include_care_plan:
            continue
        if concern.get("IsArchived") and not include_archived:
            continue
        kept.append(concern)
    return kept


def build_result(
    plan: dict,
    schedules_by_concern: dict[str, list],
    observations: dict | None = None,
    attestations_by_concern: dict[str, dict] | None = None,
    histories_by_goal: dict[str, dict] | None = None,
    sections: set[str] | None = None,
    include_care_plan: bool = False,
    include_archived: bool = False,
) -> dict:
    """Assemble the API response from the GraphQL payloads.

    Iterates EVERY health concern, not just the first. An earlier version
    read HealthConcerns[0] only and looked correct purely because patient
    1133's second concern happens to be empty — a patient with goals under
    a second concern would have silently lost them. Silent loss is the
    failure mode this project has hit repeatedly, so concerns are merged
    explicitly and each row records the concern it came from.
    """
    wanted = sections if sections is not None else {
        "summary", "attestations", "concerns", "goals", "characteristics"}
    all_concerns = (plan.get("getPatientCarePlanInternal") or {}).get("HealthConcerns") or []
    concerns = select_concerns(all_concerns, include_care_plan, include_archived)
    result: dict = {}
    # Always say what was filtered, so a caller can tell "this patient has
    # no archived plans" from "we hid them".
    result["planScope"] = {
        "returned": len(concerns),
        "totalOnRecord": len(all_concerns),
        "excludedCarePlan": sum(1 for c in all_concerns
                                if c.get("HealthConcernType") == CARE_PLAN_TYPE),
        "excludedArchived": sum(1 for c in all_concerns
                                if c.get("IsArchived")
                                and c.get("HealthConcernType") != CARE_PLAN_TYPE),
        "includeCarePlan": include_care_plan,
        "includeArchived": include_archived,
    }

    if "summary" in wanted:
        # One plan summary per concern — the DOM only ever showed the
        # Treatment Plan's, but returning all of them avoids guessing which
        # concern the caller means.
        result["planSummary"] = [
            {"health_concern_id": c.get("Id"), "health_concern": c.get("Name"),
             "plan_type": PLAN_TYPE_LABELS.get(c.get("HealthConcernType"),
                                               c.get("HealthConcernType")),
             "is_archived": bool(c.get("IsArchived")),
             **map_plan_summary(c)}
            for c in concerns
        ]

    if "concerns" in wanted:
        rows = []
        for concern in concerns:
            for row in map_concerns(concern, observations):
                rows.append({"health_concern_id": concern.get("Id"), **row})
        result["concerns"] = rows

    if "goals" in wanted:
        rows = []
        for concern in concerns:
            schedules = schedules_by_concern.get(str(concern.get("Id")), [])
            for goal in map_goals(concern, schedules):
                goal["health_concern_id"] = concern.get("Id")
                history = (histories_by_goal or {}).get(str(goal.get("goal_id")))
                if history:
                    goal["goal_progress_history"] = map_progress_history(history)
                rows.append(goal)
        result["behavioralHealthGoals"] = rows

    if "characteristics" in wanted:
        result["clientCharacteristics"] = map_client_characteristics(observations or {})

    if "attestations" in wanted:
        rows = []
        for concern_id, payload in (attestations_by_concern or {}).items():
            for row in map_attestations(payload):
                rows.append({"health_concern_id": concern_id, **row})
        result["attestationArtifacts"] = rows

    return result
