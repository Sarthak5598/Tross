"""Keeps one Chromium process AND one authenticated page warm across
requests, instead of relaunching Chromium and redoing the full
login/MFA/department chain on every single job.

Cold Chromium launch alone cost ~12-15s per request. On top of that, the
full login chain (username, password, MFA, department selection, app-shell
readiness) is several sequential network round trips — the single biggest
per-request cost, and the main reason automation felt much slower than
clicking around manually (a human logs in once, not once per click).
"""

import os

from playwright.async_api import async_playwright

import config
from automation.login import (
    select_department,
    GLOBAL_NAV_FRAME_SELECTOR,
    SEARCH_INPUT_SELECTOR,
)

_playwright = None
_browser = None
# One explicit BrowserContext, deliberately: browser.new_page() silently
# creates its OWN isolated context (separate cookie jar), and Playwright
# then refuses context.new_page() on it. So a second "tab" made that way
# is NOT logged in — proven by a probe where it got bounced to the login
# page. Every page must come from this shared context to share the single
# athena session.
_context = None
_page = None

# Set once, from the first real login's return value — see
# automation.login.login(). Reused to switch departments later without a
# full re-login (see automation.login.select_department).
_department_page_url: str | None = None
_current_department: str | None = None

APP_SHELL_READY_CHECK = "() => !!(window.AH && AH.Frames.Top.Frame().navsearchobject)"

# Playwright's own default action timeout is 30s, which quietly applied to
# any call that didn't pass an explicit timeout — that's how a failing
# "Go to Treatment Plan" click burned 30s before erroring, while every
# deliberate wait around it used our own 15s/45s constants. Setting a
# default on the page makes the implicit case consistent with the rest;
# calls that genuinely need longer (the heavy pane load) still pass their
# own explicit timeout, which overrides this.
#
# 30s, not less. This was briefly set to 20s to make the implicit case
# consistent with our explicit 15s waits — a mistake. The login page alone
# routinely takes 12-13s to render, so 20s left almost no margin and
# produced "Page.fill: Timeout 20000ms exceeded ... waiting for
# #athena-username" on slower starts. Anywhere we genuinely want to fail
# fast passes its own shorter timeout; this ceiling only exists to stop a
# call hanging indefinitely.
DEFAULT_ACTION_TIMEOUT_MS = 30_000

# How long to give the search box to prove the reused session is actually
# usable. Deliberately short — see is_logged_in().
SESSION_USABLE_TIMEOUT_MS = 4_000


def _launch_args() -> list[str]:
    """Chromium flags that only matter inside a container.

    --disable-dev-shm-usage: Docker gives /dev/shm just 64MB by default,
    and Chromium crashes on memory-heavy pages when it runs out. The
    Care Management pane is exactly that kind of page. Harmless outside
    Docker, so it's always on.

    --no-sandbox: required when running as root in a container, which is
    the default for most base images. Opt-in via env because disabling the
    sandbox on a normal host would be a needless weakening.
    """
    args = ["--disable-dev-shm-usage"]
    if os.environ.get("CHROMIUM_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
        args.append("--no-sandbox")
    return args


async def start() -> None:
    global _playwright, _browser, _context
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True, proxy=config.proxy_config(), args=_launch_args()
    )
    _context = await _browser.new_context()


async def stop() -> None:
    global _playwright, _browser, _context, _page, _department_page_url, _current_department
    _page = None
    _context = None
    _department_page_url = None
    _current_department = None
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


async def ensure_page():
    """Returns the persistent page, creating a fresh blank one if the
    browser or page is missing/dead. Does NOT log in — caller decides that
    via is_logged_in()."""
    global _browser, _page

    if _browser is None or not _browser.is_connected() or _context is None:
        await stop()
        await start()

    if _page is None or _page.is_closed():
        _page = await new_tab()

    return _page


async def new_tab():
    """A page in the shared context — i.e. a tab that shares the single
    logged-in athena session. Use this for any additional page; never
    browser.new_page(), which would get its own empty cookie jar."""
    page = await _context.new_page()
    page.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
    return page


async def reset_page() -> None:
    """Throw away the persistent page so the next job builds a fresh one
    (and logs in again). Used after a job times out: the shared page can be
    left mid-navigation in a state that is_logged_in() may still consider
    'alive', which would hand the wedged page straight to the next job."""
    global _page, _current_department

    _current_department = None
    if _page is not None and not _page.is_closed():
        try:
            await _page.close()
        except Exception:
            pass
    _page = None


async def is_logged_in(page) -> bool:
    """Whether the persistent session can actually be reused.

    Two checks, because the JS one alone is not enough. `navsearchobject`
    existing means the shell's script loaded at some point — it does NOT
    mean the UI is currently usable. A previous run can leave the shell in
    a state where that object is still present but the nav isn't rendered,
    and we'd happily "reuse" it and then fail ~20s later with
    "#searchinput ... element is not visible". That was a real, recurring
    random failure.

    So we also confirm the search box is actually visible. If it isn't,
    the session is stale: report not-logged-in and let the caller do a
    fresh login rather than driving a broken page.
    """
    try:
        if not bool(await page.evaluate(APP_SHELL_READY_CHECK)):
            return False
    except Exception:
        return False

    try:
        # Short timeout on purpose: a healthy shell has this on screen
        # immediately. Waiting longer just delays the re-login we already
        # know we need.
        await (
            page.frame_locator(GLOBAL_NAV_FRAME_SELECTOR)
            .locator(SEARCH_INPUT_SELECTOR)
            .wait_for(state="visible", timeout=SESSION_USABLE_TIMEOUT_MS)
        )
        return True
    except Exception:
        return False


def record_login(login_result: dict) -> None:
    """Called once right after a fresh login() so later requests can switch
    departments (see ensure_department) without knowing login internals."""
    global _department_page_url, _current_department
    _department_page_url = login_result["department_page_url"]
    _current_department = login_result["department"]


def current_department() -> str | None:
    return _current_department


async def ensure_department(page, department: str | None, on_step) -> None:
    """No-op if `department` is None (caller doesn't care) or already
    active. Otherwise switches via select_department — see its docstring
    for why this doesn't need a full re-login."""
    global _current_department

    if department is None or department == _current_department:
        return
    if _department_page_url is None:
        raise RuntimeError("Cannot switch department before a first login has recorded department_page_url")

    await select_department(page, _department_page_url, department, on_step)
    _current_department = department
