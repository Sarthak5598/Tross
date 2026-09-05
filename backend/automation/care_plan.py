"""Navigate from a loaded patient record into the Care Management /
Treatment Plan pane, and extract Concerns + Behavioral Health Goals
(with nested Objectives/Interventions).

Navigation chain (confirmed against real runs — see TROUBLESHOOTING.md):

1. On the patient summary page (clientsummary.esp), click the Quickview
   button. This is a real navigation within the frMain frame, but this
   legacy page's "load" event never fires cleanly (likely background
   polling) — wait_until="domcontentloaded" is what actually works.
2. The resulting briefing page's Care Management widget already surfaces
   a direct "Go to Treatment Plan »" link — click that. (We originally
   tried clicking a specific chart-nav sidebar icon by position, which
   turned out to be unreliable: right icon, but zero-size icon-font glyph
   that Playwright couldn't click cleanly, and it may already be
   active/selected by default anyway. The direct link avoids all of that.)
3. The pane that opens renders inside an *open* shadow root
   (`<template shadowrootmode="open">`). Playwright's locators pierce open
   shadow roots automatically, so no special handling is needed for
   extraction.
4. Extract Concerns and Behavioral Health Goals from the now-open pane.
"""

QUICKVIEW_BUTTON_SELECTOR = (
    "#quickview-react-version > div > div:nth-child(1) > table > tbody > "
    "tr:nth-child(1) > td:nth-child(2) > table > tbody > tr:nth-child(1) > "
    "td:nth-child(3) > input"
)

# The Quickview/briefing page's Care Management widget already surfaces a
# direct "Go to Treatment Plan »" link — far simpler and more robust than
# the sidebar-icon + slideout-button chain we originally tried (that icon
# turned out to already be selected/active by default, with no reliable
# click target). Confirmed present via a real screenshot of the briefing
# page.
TREATMENT_PLAN_LINK_TEXT = "Go to Treatment Plan"

# Inside the Care Management shadow DOM.
CONCERN_ITEM_SELECTOR = ".acc_c_hc-list-item"
GOAL_ITEM_SELECTOR = ".acc_c_cm-goal-panel-twopane_view__goal-panel-content"
SHOW_MORE_BUTTON_TEXT = "Show More"

NAV_TIMEOUT_MS = 15_000
PANE_LOAD_TIMEOUT_MS = 45_000


async def open_care_management_pane(page, on_step) -> None:
    # The patient chart (like the search box) lives inside the frMain
    # frame, not the top-level page — see TROUBLESHOOTING.md #6 for why
    # plain page.click()/wait_for_selector() silently can't find anything
    # here.
    from automation.patient_search import PATIENT_FRAME_NAME, _wait_for_frame

    frame = await _wait_for_frame(page, PATIENT_FRAME_NAME, NAV_TIMEOUT_MS)

    # wait_until="load" hangs — this legacy page's "load" event appears to
    # never fire cleanly (likely background polling/long-lived requests).
    # "domcontentloaded" is enough to safely interact with the page.
    async with frame.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS):
        await frame.click(QUICKVIEW_BUTTON_SELECTOR, timeout=NAV_TIMEOUT_MS)
    await on_step(f"Opened Quickview — landed on {frame.url}")

    # Re-fetch: a frame navigation can leave the old Frame handle stale,
    # and page.frame() is an instant check, not a wait — see
    # patient_search._wait_for_frame's docstring for why this matters.
    frame = await _wait_for_frame(page, PATIENT_FRAME_NAME, NAV_TIMEOUT_MS)

    # This link is only rendered when the patient actually has a Treatment
    # Plan in the *currently active department*. When they don't, the click
    # just times out, which used to surface as a bare
    # "Locator.click: Timeout 30000ms exceeded" with no hint about the real
    # cause. Name the likely reason, and the department, in the error.
    from automation import browser_pool

    try:
        await frame.get_by_text(TREATMENT_PLAN_LINK_TEXT).click(timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        department = browser_pool.current_department() or "unknown"
        raise RuntimeError(
            f"Could not open the Treatment Plan: no '{TREATMENT_PLAN_LINK_TEXT}' link "
            f"appeared within {NAV_TIMEOUT_MS // 1000}s. The patient most likely has no "
            f"Treatment Plan in the active department ({department!r}) — try passing an "
            f"explicit `department`. Underlying error: {exc}"
        ) from exc
    await on_step("Clicked 'Go to Treatment Plan'")

    # Shadow root content loads async and this widget is heavy — confirmed
    # via screenshot that the pane opens immediately but shows a loading
    # spinner for a while before content renders, so this needs more time
    # than the other, lighter waits in this flow.
    await frame.wait_for_selector(GOAL_ITEM_SELECTOR, timeout=PANE_LOAD_TIMEOUT_MS)
    await on_step("Care Management pane loaded")


def _text(locator) -> str:
    return (locator.inner_text() or "").strip()


async def _label_value_pairs(scope) -> dict:
    """Parses .acc_c_label-value-display pairs (label-text + value-text)
    into a {label: value} dict, for rows like:
    'As evidenced by: TESTB' or 'Start date: 09-02-2026 | Target date: 10-02-2026'.
    """
    pairs = {}
    displays = await scope.locator(".acc_c_label-value-display").all()
    for display in displays:
        labels = await display.locator(".acc_c_label-value-display__label-text").all_inner_texts()
        values = await display.locator(".acc_c_label-value-display__value-text").all_inner_texts()
        for label, value in zip(labels, values):
            pairs[label.strip().rstrip(":").strip()] = value.strip()
    return pairs


async def _attribution(scope) -> dict:
    lines = await scope.locator(".acc_c_cm-attribution-label").all_inner_texts()
    result = {}
    for line in lines:
        if line.startswith("Added by"):
            result["added_by"] = line.replace("Added by", "").strip(" |")
        elif line.startswith("Last action by"):
            result["last_action_by"] = line.replace("Last action by", "").strip(" |")
    return result


async def _associated_diagnoses(item) -> list[str]:
    """Best-effort: for this patient every concern's diagnoses div only
    ever contained the bare label with no value ("Associated diagnoses",
    nothing after it), so we've never seen a populated example. This grabs
    any text beyond the label itself, on the assumption a populated one
    lists diagnosis codes/names as additional text or child elements
    inside the same div.
    """
    div = item.locator(".acc_c_hc-list-item__diagnoses")
    if not await div.count():
        return []
    full_text = (await div.inner_text()).strip()
    label_text = (await div.locator(".acc_c_hc-list-item__diagnoses-label-text").inner_text()).strip()
    remainder = full_text.replace(label_text, "", 1).strip()
    return [line.strip() for line in remainder.split("\n") if line.strip()]


async def extract_concerns(page) -> list[dict]:
    concerns = []
    items = await page.locator(CONCERN_ITEM_SELECTOR).all()
    for item in items:
        title = (await item.locator(".acc_c_hc-list-item__title").inner_text()).strip()
        evidenced = await _label_value_pairs(item.locator(".acc_c_hc-list-item__evidenced-by"))
        goals_summary = await _label_value_pairs(item.locator(".acc_c_hc-list-item__goals-summary"))
        diagnoses = await _associated_diagnoses(item)
        attribution = await _attribution(item)
        concerns.append(
            {
                "title": title,
                "evidenced_by": evidenced.get("As evidenced by"),
                "associated_diagnoses": diagnoses,
                "goals": goals_summary.get("Goals"),
                **attribution,
            }
        )
    return concerns


async def extract_treatment_plan_summary(page) -> dict:
    """Date of next review, plan added by, last action on plan by — from
    the top of the Treatment Plan tab, above Concerns. Confirmed against
    real HTML.
    """
    widget = page.locator(".acc_c_plan-overview-widget")
    if not await widget.count():
        return {"review_date": None, "plan_added_by": None, "last_action_by": None}

    review_date = await widget.locator("#plan-reviewduedate").get_attribute("value")
    pairs = await _label_value_pairs(widget)
    return {
        "review_date": review_date,
        "plan_added_by": pairs.get("Plan added by"),
        "last_action_by": pairs.get("Last action on plan by"),
    }


async def extract_attestation_artifacts(page) -> list[dict]:
    """Empty-state ("There are no attestations") confirmed against real
    HTML. Populated-item shape is best-effort — this patient had none to
    verify against.
    """
    list_container = page.locator(".acc_c_cm-attestation-list")
    if not await list_container.count() or await list_container.locator(EMPTY_CARD_LABEL_SELECTOR).count():
        return []

    items = []
    for item in await list_container.locator(".acc_c_list-item-panel, .acc_c_snaps-panel").all():
        text_lines = (await item.inner_text()).strip().split("\n")
        title = text_lines[0] if text_lines else ""
        attribution = await _attribution(item)
        items.append({"title": title, **attribution})
    return items


CLIENT_CHARACTERISTIC_GROUPS = ["Strengths", "Needs", "Abilities", "Preferences", "Supports"]


async def extract_client_characteristics(page) -> dict:
    """Strengths / Needs / Abilities / Preferences / Supports (SNAPS),
    below Behavioral Health Goals on the Treatment Plan tab. Structure
    confirmed against real HTML.
    """
    result = {}
    for group in CLIENT_CHARACTERISTIC_GROUPS:
        key = group.lower()
        sub_widget = page.locator(".acc_c_snaps-sub-widget").filter(
            has=page.locator(".acc_c_snaps-sub-widget__label", has_text=group)
        )
        if not await sub_widget.count():
            result[key] = []
            continue

        entries = []
        for panel in await sub_widget.locator(".acc_c_snaps-panel").all():
            title = (await panel.locator(".acc_c_snaps-panel__title").inner_text()).strip()
            attribution = await _attribution(panel)
            entries.append({"title": title, **attribution})
        result[key] = entries
    return result


EXPAND_TOGGLE_SELECTOR = ".acc_c_cm-goal-panel-twopane_view__expand-toggle-text"
EXPAND_TIMEOUT_MS = 10_000

# The flat wait before reading a goal's expanded detail. Two attempts to
# replace this with something smarter have BOTH regressed to empty
# Objectives/Interventions on every goal — polling the Baseline text
# (TROUBLESHOOTING.md #16) and waiting on the goal-detail GraphQL response
# (#18). Change it only with a full re-verification against the known-good
# counts; the measured upside is ~3-4s per run, which is not worth another
# silent data-loss bug.
GOAL_DETAIL_WAIT_MS = 4_000


async def _expand_goal(page, panel, on_step=None) -> None:
    """Expand one goal panel's "Show More", scoped to that panel only.

    Collecting every "Show More" button up front (via .all()) and clicking
    them in a loop breaks: expanding goal 1 shifts the layout, so the
    locators captured for goals 2+ can end up stale or pointing at the
    wrong element by the time we get to them. Doing this one panel at a
    time, scoped to that panel's own toggle button, avoids the problem
    entirely — each click only ever affects the panel we already have a
    handle on.
    """
    toggle = panel.locator(EXPAND_TOGGLE_SELECTOR)
    if not await toggle.count():
        return
    label = (await toggle.inner_text()).strip()
    if label != SHOW_MORE_BUTTON_TEXT:
        return  # already expanded (reads "Show Less"), or unexpected state

    await toggle.click()

    # Re-check the same scoped locator until its label flips — confirms the
    # expansion actually happened rather than assuming click == expanded.
    await panel.locator(EXPAND_TOGGLE_SELECTOR).get_by_text("Show Less", exact=True).wait_for(
        timeout=EXPAND_TIMEOUT_MS
    )
    if on_step:
        await on_step("Expanded a goal panel")


async def extract_behavioral_health_goals(page, on_step=None, expand: bool = True) -> list[dict]:
    """`expand=False` skips clicking "Show More" and reading the shared
    detail panel for every goal — i.e. skips Baseline/Treatment
    Modalities/Objectives/Interventions/Goal Progress History entirely.
    All other fields (status, priority, title, statement, targeted
    concerns, dates, attribution) live on the goal's own list item and are
    available either way. This is the single slowest part of the whole
    flow (a ~5s expand+read per goal), so skipping it is the main lever
    for a materially faster/shorter response.
    """
    count = await page.locator(GOAL_ITEM_SELECTOR).count()

    goals = []
    for i in range(count):
        # Re-locate by index each iteration rather than reusing a captured
        # list — same reasoning as _expand_goal's docstring.
        panel = page.locator(GOAL_ITEM_SELECTOR).nth(i)

        if expand:
            try:
                await _expand_goal(page, panel, on_step)
            except Exception as exc:
                if on_step:
                    await on_step(f"Could not expand goal {i}: {exc}")

        status = await panel.locator(
            ".acc_c_cm-goal-panel-twopane_view__goal-status"
        ).get_attribute("title")
        priority = (
            await panel.locator(".acc_c_cm-goal-panel-twopane_view__goal-priority").inner_text()
        ).strip()
        title = (
            await panel.locator(".acc_c_cm-goal-panel-twopane_view__goal-title-row h1").inner_text()
        ).strip()
        statement_loc = panel.locator(".acc_c_cm-goal-panel-twopane_view__goal-statement-row")
        statement = (await statement_loc.inner_text()).strip() if await statement_loc.count() else None

        detail = await _label_value_pairs(
            panel.locator(".acc_c_cm-goal-panel-twopane_view__goal-detail-content")
        )
        attribution = await _attribution(
            panel.locator(".acc_c_cm-goal-panel-twopane_view__goal-footer-attribution")
        )

        # Expanding a goal doesn't nest Baseline/Objectives/Interventions
        # inside that goal's own list item — it populates a single shared
        # detail panel elsewhere on the page (a master-detail pattern), and
        # whichever goal we most recently expanded is the one currently
        # shown there. So we read it right after expanding, before moving
        # on to the next goal. Confirmed via a real HTML dump (see
        # TROUBLESHOOTING.md #9) — not a guess.
        expanded_details = await _extract_expanded_goal_details(page) if expand else {}

        goals.append(
            {
                "status": status,
                "priority": priority,
                "title": title,
                "client_statement": statement,
                "targeted_concerns": detail.get("Targeted concerns"),
                "start_date": detail.get("Start date"),
                "review_date": detail.get("Review date"),
                "target_date": detail.get("Target date"),
                **attribution,
                **expanded_details,
            }
        )
    return goals


DETAIL_PANEL_SELECTOR = ".acc_c_goal-expanded-view"
BASELINE_CONTENT_SELECTOR = ".acc_c_goal-expanded-view__expanded-goal-content"
CARD_TITLE_SELECTOR = ".acc_c_careplan-container-card__title"
EMPTY_CARD_LABEL_SELECTOR = ".acc_c_empty-card__label"


async def _extract_task_card(page, heading_text: str) -> list[dict]:
    """Extracts one 'Objectives' or 'Interventions' card from the shared
    detail panel. Both empty-state and populated-item shape are now
    confirmed against real HTML (a goal with actual objectives) — no
    longer a guess. Item title lives in a specific h1, not just "the
    first line of text" (that first line is actually the "Behavioral
    Objective"/"Behavioral Intervention" label, not the title).
    """
    heading = page.locator(CARD_TITLE_SELECTOR).filter(has_text=heading_text)
    if not await heading.count():
        return []
    # NOTE: contains(@class, 'acc_c_careplan-container-card') is too loose —
    # BEM-style child classes like 'acc_c_careplan-container-card__header-col'
    # also contain that substring, so a naive contains() match grabs a
    # shallow child span instead of the actual card wrapper several levels
    # up. Match the exact class token instead (padded-spaces trick).
    card = heading.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
        "' acc_c_careplan-container-card ')][1]"
    )
    if await card.locator(EMPTY_CARD_LABEL_SELECTOR).count():
        return []

    items = []
    for item in await card.locator(".acc_c_list-item-panel").all():
        title = (
            await item.locator(".acc_c_task-panel-twopane_view__task-title-row h1").inner_text()
        ).strip()
        dates = await _label_value_pairs(item)
        attribution = await _attribution(item)
        items.append({"title": title, **dates, **attribution})
    return items


async def _extract_expanded_goal_details(page) -> dict:
    panel = page.locator(DETAIL_PANEL_SELECTOR)
    if not await panel.count():
        return {"baseline_description": None, "objectives": [], "interventions": []}

    # Selecting a goal re-fetches Baseline/Objectives/Interventions/Progress
    # asynchronously, so we wait before reading. This flat delay is the
    # only approach that has ever verified clean against real data — see
    # GOAL_DETAIL_WAIT_MS for the two smarter attempts that both regressed.
    await page.wait_for_timeout(GOAL_DETAIL_WAIT_MS)

    baseline = await _label_value_pairs(panel.locator(BASELINE_CONTENT_SELECTOR))

    return {
        "baseline_description": baseline.get("Baseline description"),
        "treatment_modalities": baseline.get("Treatment modalities"),
        "goal_progress_history": await _extract_goal_progress_history(page, panel),
        "objectives": await _extract_task_card(page, "Objectives"),
        "interventions": await _extract_task_card(page, "Interventions"),
    }


PROGRESS_HISTORY_TOGGLE_SELECTOR = ".fe_c_button__text:has-text('Show Progress History')"
PROGRESS_HISTORY_ROW_SELECTOR = ".acc_c_goal-progress-status-list__progress-status-row"
PROGRESS_HISTORY_TITLE_SELECTOR = ".acc_c_goal-progress-status-list__status-history-title"
PROGRESS_HISTORY_REASON_SELECTOR = ".acc_c_goal-progress-status-list__status-history-reason"
PROGRESS_HISTORY_STATUS_TAG_SELECTOR = ".acc_c_cm-goal-progress-panel__goal-status"


async def _extract_goal_progress_history(page, panel) -> list[dict]:
    """Confirmed against real populated data: the 'Goal Progress Status'
    card shows only the latest entry until 'Show Progress History (N
    items)' is clicked, which expands a list of
    .acc_c_goal-progress-status-list__progress-status-row entries (each has
    a date, a status tag, an optional reason, and a
    .acc_c_cm-attribution-label — same attribution pattern used
    everywhere else on this page). Expanding first means we always read
    the full history rather than just the current status.
    """
    toggle = panel.locator(PROGRESS_HISTORY_TOGGLE_SELECTOR)
    if await toggle.count() and "show" in (await toggle.first.inner_text()).strip().lower():
        await toggle.first.click()
        await page.wait_for_timeout(500)

    rows = panel.locator(PROGRESS_HISTORY_ROW_SELECTOR)
    if not await rows.count():
        return []

    entries = []
    for row in await rows.all():
        title_text = (await row.locator(PROGRESS_HISTORY_TITLE_SELECTOR).first.inner_text()).strip()
        date, _, _ = title_text.partition("|")
        status_tag = row.locator(PROGRESS_HISTORY_STATUS_TAG_SELECTOR)
        status = (
            (await status_tag.first.get_attribute("title") or await status_tag.first.inner_text()).strip()
            if await status_tag.count()
            else None
        )
        reason = (await row.locator(PROGRESS_HISTORY_REASON_SELECTOR).inner_text()).strip() or None
        attribution = await _attribution(row)
        entries.append({"date": date.strip(), "status": status, "reason": reason, **attribution})
    return entries
