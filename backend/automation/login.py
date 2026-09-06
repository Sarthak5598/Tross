"""Athena sandbox login: username/password, then a second-step TOTP form.

Both steps reuse the same widget footer button and (confusingly) the same
`#athena-password` id for their input field — confirmed only one instance
of that id exists in the DOM at a time (login form fully unmounts before
the MFA form mounts), so plain `page.fill` per step is safe.
"""

import pyotp

USERNAME_SELECTOR = "#athena-username"
PASSWORD_SELECTOR = "#athena-password"

# The password -> MFA transition re-enables the shared input. Generous,
# because it is a page transition on a 1GB instance, and failing here
# costs a whole login.
MFA_READY_TIMEOUT_MS = 60_000
SUBMIT_BUTTON_SELECTOR = "#athena-o-form-button-bar > div.fe_c_root.fe_f_all > div > button"

# Athena redirects through identity.athenahealth.com during MFA, then lands
# back on a preview.athenahealth.com page (choosedepartment.esp, at least for
# multi-department accounts) once credentials + TOTP are accepted. We treat
# leaving the identity domain as "login confirmed" — department selection
# (if needed) is a separate step layered on top once we have its selectors.
POST_LOGIN_URL_PATTERN = "**://preview.athenahealth.com/**"

# Department-choice page reached after login. For now we always accept
# whatever department is pre-selected by default and just submit — picking
# a specific department (via an enum-mapped query param) is a planned
# follow-up once we know the option values and whether the app shell differs
# per department.
DEPARTMENT_SELECT_SELECTOR = "#DEPARTMENTID"
DEPARTMENT_SUBMIT_SELECTOR = "#loginbutton"

# The app shell after department selection is a frameset — the search box
# (and everything else we care about) lives inside this child iframe, not
# on the top-level page. Every later interaction needs page.frame_locator(),
# not plain page.locator()/wait_for_selector().
GLOBAL_NAV_FRAME_SELECTOR = "iframe#GlobalNav"
SEARCH_INPUT_SELECTOR = "#searchinput"

LOGIN_TIMEOUT_MS = 15_000


async def _wait_for_app_shell(page, on_step) -> None:
    await page.wait_for_selector(GLOBAL_NAV_FRAME_SELECTOR, timeout=LOGIN_TIMEOUT_MS)
    await page.frame_locator(GLOBAL_NAV_FRAME_SELECTOR).locator(SEARCH_INPUT_SELECTOR).wait_for(
        timeout=LOGIN_TIMEOUT_MS
    )

    # The search box's onkeyup handler delegates to a client-side object
    # (navsearchobject) that isn't ready the instant the app shell appears —
    # typing before it's ready silently no-ops (see TROUBLESHOOTING.md #7).
    await page.wait_for_function(
        "() => !!AH.Frames.Top.Frame().navsearchobject", timeout=LOGIN_TIMEOUT_MS
    )
    await on_step("App shell loaded")


async def select_department(page, department_page_url: str, department_label: str, on_step) -> None:
    """Switch the active department for an already-authenticated session.
    Confirmed (real screenshot showing "Last login: ...") that revisiting
    choosedepartment.esp directly does NOT require re-entering
    credentials/MFA — it just re-shows the department dropdown for the
    session that's already logged in. Far cheaper than a full re-login.
    """
    await page.goto(department_page_url)
    await page.wait_for_selector(DEPARTMENT_SELECT_SELECTOR, timeout=LOGIN_TIMEOUT_MS)

    try:
        await page.select_option(DEPARTMENT_SELECT_SELECTOR, label=department_label)
    except Exception:
        options = await page.locator(f"{DEPARTMENT_SELECT_SELECTOR} option").all_inner_texts()
        raise ValueError(
            f"Unknown department {department_label!r}. Available: {options}"
        )
    await on_step(f"Selected department: {department_label}")

    await page.click(DEPARTMENT_SUBMIT_SELECTOR)
    await on_step(f"Submitted department selection ({department_label})")

    await _wait_for_app_shell(page, on_step)


async def login(page, username: str, password: str, totp_secret: str, on_step) -> dict:
    await page.fill(USERNAME_SELECTOR, username)
    await on_step(f"Filled username ({username})")

    await page.fill(PASSWORD_SELECTOR, password)
    await on_step("Filled password")

    await page.click(SUBMIT_BUTTON_SELECTOR)
    await on_step("Submitted login form")

    # The MFA step reuses the SAME input, so "the field exists" does not
    # mean the page is ready for the code — right after submit the field is
    # still present, still holding the password, and disabled while the
    # form processes. Filling then fails with "element is not enabled"
    # until it times out.
    #
    # Wait for it to be genuinely ready: enabled, and no longer carrying
    # the password. On a small instance that transition can take well over
    # the default timeout, which is what a real failure looked like.
    await page.wait_for_selector(PASSWORD_SELECTOR, timeout=LOGIN_TIMEOUT_MS)
    await page.wait_for_function(
        """() => {
            const el = document.querySelector('#athena-password');
            return el && !el.disabled && el.value === '';
        }""",
        timeout=MFA_READY_TIMEOUT_MS,
    )
    await on_step("MFA step loaded")

    # Generated AFTER the wait, never before. Codes are valid for a single
    # 30-second window, so one minted ahead of a slow transition can be
    # expired by the time it is submitted — a failure that looks like bad
    # credentials rather than bad timing.
    code = pyotp.TOTP(totp_secret).now()
    await page.fill(PASSWORD_SELECTOR, code)
    await on_step("Filled TOTP code")

    await page.click(SUBMIT_BUTTON_SELECTOR)
    await on_step("Submitted MFA form")

    # wait_until="domcontentloaded" is load-bearing, not a style choice:
    # wait_for_url defaults to waiting for the "load" event, and this
    # legacy app's "load" frequently never fires (background polling /
    # long-lived requests keep it pending — same root cause documented in
    # TROUBLESHOOTING.md #9, which is why the Quickview navigation passes
    # this too). With the default, this step times out after MFA with
    # "waiting for navigation ... until 'load'" even though the URL
    # already matched and the page is perfectly usable.
    await page.wait_for_url(
        POST_LOGIN_URL_PATTERN, timeout=LOGIN_TIMEOUT_MS, wait_until="domcontentloaded"
    )
    await on_step(f"Authenticated — landed on {page.url}")

    # Captured so a later department switch can revisit this exact URL
    # without redoing username/password/MFA (see select_department above).
    department_page_url = page.url

    await page.wait_for_selector(DEPARTMENT_SUBMIT_SELECTOR, timeout=LOGIN_TIMEOUT_MS)
    default_department = (
        await page.locator(f"{DEPARTMENT_SELECT_SELECTOR} option:checked").inner_text()
    ).strip()
    await on_step("Department selection page loaded")

    await page.click(DEPARTMENT_SUBMIT_SELECTOR)
    await on_step(f"Submitted department selection ({default_department})")

    await _wait_for_app_shell(page, on_step)
    await on_step("Login flow complete")

    return {"department_page_url": department_page_url, "department": default_department}
