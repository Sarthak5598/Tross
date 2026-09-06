"""Navigate from a loaded patient record into the Care Management pane.

This module used to extract the Treatment Plan out of the rendered DOM.
It no longer does — all patient data now comes from the Care Management
GraphQL API (see automation/graphql.py and docs/ENDPOINTS.md), which is
faster, concurrent-safe, and fails loudly instead of silently returning
an empty list.

What survives is the navigation itself, for one reason: opening this pane
is what makes the app issue GraphQL requests, and those requests are
where we harvest the auth headers. So this is now purely a token-warm-up
path, run once at startup, never in a request.

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

# Still used as the "pane has finished rendering" signal.
GOAL_ITEM_SELECTOR = ".acc_c_cm-goal-panel-twopane_view__goal-panel-content"

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

    # Two distinct failure modes here, and they need different messages:
    #
    #  (a) The link isn't rendered at all — the patient has no Treatment
    #      Plan in the *currently active department*. Then the locator
    #      never resolves.
    #  (b) The link resolves and passes every actionability check, but the
    #      click itself never lands. Seen for real on a small EC2 instance:
    #      Playwright logged "element is visible, enabled and stable /
    #      done scrolling" and then timed out anyway — something intercepts
    #      the pointer, or the retry loop is just too slow on a starved
    #      CPU. Blaming the department there (as this used to) sends you
    #      down completely the wrong path.
    #
    # For (b), fall back to dispatching the click via JS, which bypasses
    # both interception and the actionability re-checks. That's normally a
    # thing to avoid — it can "click" something invisible — but here we've
    # already confirmed the element resolved and was visible and stable,
    # so the risk that we're hitting the wrong thing is minimal.
    from automation import browser_pool

    link = frame.get_by_text(TREATMENT_PLAN_LINK_TEXT)
    try:
        await link.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        department = browser_pool.current_department() or "unknown"
        raise RuntimeError(
            f"Could not open the Treatment Plan: no '{TREATMENT_PLAN_LINK_TEXT}' link "
            f"appeared within {NAV_TIMEOUT_MS // 1000}s. The patient most likely has no "
            f"Treatment Plan in the active department ({department!r}) — try passing an "
            f"explicit `department`. Underlying error: {exc}"
        ) from exc

    try:
        await link.click(timeout=NAV_TIMEOUT_MS)
        await on_step("Clicked 'Go to Treatment Plan'")
    except Exception as exc:
        await on_step(f"Normal click failed ({type(exc).__name__}), retrying via JS dispatch")
        await link.evaluate("el => el.click()")
        await on_step("Clicked 'Go to Treatment Plan' (JS fallback)")

    # Shadow root content loads async and this widget is heavy — confirmed
    # via screenshot that the pane opens immediately but shows a loading
    # spinner for a while before content renders, so this needs more time
    # than the other, lighter waits in this flow.
    await frame.wait_for_selector(GOAL_ITEM_SELECTOR, timeout=PANE_LOAD_TIMEOUT_MS)
    await on_step("Care Management pane loaded")

