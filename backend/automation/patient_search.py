"""Patient search: type an ID into the global search box, then explicitly
pick the "Patient ID" category from the dropdown that appears (rather than
just hitting the search icon) — plain text can be ambiguous between a
Claim ID and a Patient ID, so we disambiguate by category on purpose.

Getting this right took a lot of debugging — see TROUBLESHOOTING.md #6-#7
for the full story. Summary of the non-obvious parts:

1. The search *input* lives inside the GlobalNav iframe, so typing into it
   goes through page.frame_locator(GLOBAL_NAV_FRAME_SELECTOR).
2. That input holds literal placeholder text as its real `value` (an old
   JS pattern, not a native placeholder attribute) which its own focus
   handler fails to clear for a synthetic click — so we clear it ourselves
   (Ctrl+A, Delete) rather than relying on the site's own logic.
3. `page.fill()` never opens the dropdown at all — the input's onkeyup
   handler needs real per-character key events, so we use
   press_sequentially() instead.
4. The dropdown's rendering also depends on a client-side object
   (navsearchobject) that isn't ready the instant the app shell first
   loads; automation/login.py waits for it before returning.
5. The dropdown itself has **no per-row DOM elements** — confirmed via
   plain DOM queries, shadow-DOM-recursive queries, and an accessibility
   snapshot, all coming up empty despite the option being visibly on
   screen. The accessibility snapshot showed the entire menu as a single
   flattened text node, meaning it's rendered as one text blob with
   coordinate-based click handling (a legacy custom-dropdown pattern) —
   there is nothing to select with a CSS/text locator. Instead we use the
   browser's own `window.find()` (the API behind Ctrl+F) to locate exactly
   where the text renders on screen, then click that pixel coordinate.
6. Selecting the category alone doesn't submit the search — an explicit
   Enter on the search box (which Playwright's `.press()` focuses first
   regardless of current focus state) is what actually navigates.
"""

import asyncio

from automation.login import GLOBAL_NAV_FRAME_SELECTOR, SEARCH_INPUT_SELECTOR

PATIENT_ID_OPTION_TEXT = "Patient ID"

# The patient page loads into this specific named iframe (confirmed via a
# debug run: its URL becomes .../client/clientsummary.esp?ID=<patient_id>
# after a successful search).
PATIENT_FRAME_NAME = "frMain"

# Present once a patient record has loaded — seen in the DOM on the patient
# summary/registration page.
PATIENT_LOADED_SELECTOR = ".pb_c_patient-id-module"

# What athena renders instead when the ID doesn't resolve. Note it
# deliberately conflates "no such patient" with "you can't see this one" —
# it will not tell us which, so neither can we. Matched on a substring
# because the full sentence embeds the patient number.
PATIENT_NOT_FOUND_TEXT = "does not exist, or you do not have permission"

# A third outcome, neither chart nor "not found": the person exists but is
# registered under a different patient record in another provider group.
# athena renders an interstitial ("...but under a different patient
# record. #X ... is the patient record in the current provider group.
# Proceed to #Y ..."). Left unhandled this just hangs until the timeout,
# exactly like the not-found case used to.
PATIENT_OTHER_RECORD_TEXT = "registered in this provider group, but under a different patient record"

# Sized for a slow day, not a fast one. This runs on the startup path
# where a failure means the service comes up with no session at all, and
# 15s was not enough to find frMain on a loaded machine even though the
# login itself had completed fine.
SEARCH_TIMEOUT_MS = 120_000


class PatientNotFoundError(Exception):
    """The searched patient ID didn't resolve to a viewable record.

    Distinct from an automation failure: nothing is broken, the input just
    didn't match anything the logged-in user can see. Callers should treat
    this as a bad-input condition, not a retryable error.
    """


class PatientRecordMismatchError(Exception):
    """The person exists, but this ID belongs to a different provider
    group's record than the one currently active.

    Deliberately NOT auto-followed. athena offers a "Proceed to #N" link,
    but taking it would return a different provider group's record than
    the caller asked for — the same silent-wrong-data failure mode this
    codebase has been bitten by repeatedly. Surface it and let the caller
    decide.
    """


async def _wait_for_frame(page, name: str, timeout_ms: int):
    """page.frame(name=...) is an instant snapshot check, not a wait — it
    can return None if called in the exact instant a frame is mid
    detach/reattach during a navigation, even though the frame reappears
    moments later. Confirmed as the real cause of repeated
    "Could not find frame named 'frMain'" failures: we were calling
    page.frame() with zero wait immediately after triggering the search
    navigation. Poll instead of checking once.
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while True:
        frame = page.frame(name=name)
        if frame is not None:
            return frame
        if asyncio.get_event_loop().time() >= deadline:
            raise RuntimeError(f"Could not find frame named {name!r} after {timeout_ms}ms")
        await asyncio.sleep(0.2)


async def _locate_option_coords(page, text: str) -> dict | None:
    return await page.evaluate(
        """(text) => {
            const sel = window.getSelection();
            sel.removeAllRanges();
            const found = window.find(text, false, false, false, false, false, false);
            if (!found) return null;
            const range = sel.getRangeAt(0);
            const rect = range.getBoundingClientRect();
            sel.removeAllRanges();
            return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
        }""",
        text,
    )


async def search_patient(page, patient_id: str, on_step) -> None:
    search_box = page.frame_locator(GLOBAL_NAV_FRAME_SELECTOR).locator(SEARCH_INPUT_SELECTOR)

    await search_box.click()
    await search_box.press("Control+A")
    await search_box.press("Delete")
    await search_box.press_sequentially(patient_id, delay=100)
    await on_step(f"Typed patient ID ({patient_id}) into search")

    coords = await _locate_option_coords(page, PATIENT_ID_OPTION_TEXT)
    if not coords:
        raise RuntimeError(f"Could not locate '{PATIENT_ID_OPTION_TEXT}' option in search dropdown")

    await page.mouse.click(coords["x"], coords["y"])
    await on_step("Selected 'Patient ID' category from search dropdown")

    # Wait for the search to actually navigate frMain before inspecting it.
    # Skipping this is a correctness bug, not just a slow path: on a reused
    # session frMain still shows the PREVIOUS patient, so
    # PATIENT_LOADED_SELECTOR matches immediately and we conclude "loaded"
    # against stale content — reporting success ~0.4s after submitting, and
    # potentially extracting the wrong patient's record. Caught by a real
    # run where a nonexistent ID reported "Patient record loaded".
    patient_frame = await _wait_for_frame(page, PATIENT_FRAME_NAME, SEARCH_TIMEOUT_MS)
    async with patient_frame.expect_navigation(
        wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT_MS
    ):
        await search_box.press("Enter")
    await on_step("Submitted patient search")

    # Re-fetch: a frame navigation can leave the old handle stale — see
    # _wait_for_frame's docstring.
    patient_frame = await _wait_for_frame(page, PATIENT_FRAME_NAME, SEARCH_TIMEOUT_MS)

    # A bad patient ID doesn't hang or error — athena renders a plain
    # "Patient #N does not exist, or you do not have permission to view
    # this record." message. Waiting only on the success selector meant
    # that case burned the full timeout and then surfaced as a generic
    # Playwright error, hiding a perfectly clear message from the app.
    # Race the two outcomes and report whichever actually happened.
    loaded = patient_frame.locator(PATIENT_LOADED_SELECTOR)
    not_found = patient_frame.get_by_text(PATIENT_NOT_FOUND_TEXT)
    other_record = patient_frame.get_by_text(PATIENT_OTHER_RECORD_TEXT)
    await loaded.or_(not_found).or_(other_record).first.wait_for(timeout=SEARCH_TIMEOUT_MS)

    if await other_record.count():
        # Include athena's own explanation — it names both record ids, so
        # the caller can decide which one they actually wanted.
        try:
            detail = " ".join((await patient_frame.locator("body").inner_text()).split())[:400]
        except Exception:
            detail = ""
        raise PatientRecordMismatchError(
            f"Patient {patient_id!r} belongs to a different provider group than the one "
            f"currently active, so athenahealth did not open the chart. Not following its "
            f"'Proceed to' link automatically, since that would return a different "
            f"provider group's record than requested. athenahealth said: {detail}"
        )

    if await not_found.count():
        from automation import browser_pool

        department = browser_pool.current_department() or "unknown"
        raise PatientNotFoundError(
            f"Patient {patient_id!r} does not exist, or is not viewable by this account "
            f"in department {department!r}. (athenahealth does not distinguish between "
            f"the two.) Patient visibility is department-scoped — if you expect this "
            f"patient to exist, try an explicit `department`."
        )

    # Confirm the page we're about to scrape is actually the patient that
    # was asked for. The frame URL carries the id (…/clientsummary.esp?…
    # ID=<patient_id>&…). This is a medical record — extracting a
    # *different* patient's data because of a slow or partial navigation
    # would be far worse than failing, so verify rather than assume.
    if f"ID={patient_id}" not in patient_frame.url:
        raise RuntimeError(
            f"Loaded page does not appear to be patient {patient_id!r} — frame URL is "
            f"{patient_frame.url!r}. Refusing to extract, in case this is a stale or "
            f"wrong record."
        )

    await on_step("Patient record loaded")
