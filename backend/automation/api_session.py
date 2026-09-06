"""Ties the browser to the API: log in once, then serve HTTP clients.

The browser exists only to hold a logged-in session and surrender its
request headers. No patient data is read from the page.

    login (once, ~30s)  ->  headers captured  ->  CareManagementClient
                              ^                        |
                              |                   plain HTTP, concurrent
                         refreshed by reload
                         when the token ages out

Two costs shape the design. A login takes ~30 seconds and needs MFA, so it
happens at startup and effectively never again. The token lives only ~5
minutes, so it is refreshed constantly — but refreshing does NOT re-login:
the browser session outlives the token and re-reading a request header is
enough.

Note the department header does not affect care-plan queries — verified by
issuing the same query under three different departments and getting
byte-identical responses. It is still sent for fidelity with the app, but
nothing here needs to switch it.
"""

import asyncio
import time

import config
from automation import browser_pool
from automation.graphql import CareManagementClient
from automation.login import login
from automation.patient_search import search_patient
from automation.care_plan import open_care_management_pane
from automation.token_manager import TokenManager, decode_expiry
from automation.token_source import HeaderCapture, mint_token
from automation.http_login import AthenaHttpSession, LoginFailed

# Don't reuse a token with less than this left — a request that starts
# near expiry could outlive it mid-flight.
MIN_TOKEN_LIFE_S = 60

# Re-login when the session has less than this much idle budget left.
# athenahealth reports 1800s; five minutes of headroom is generous against
# a login that takes ~15s.
SESSION_RENEW_MARGIN_S = 300

# Opening any patient's Care Management pane makes the app issue the
# GraphQL calls we harvest headers from. The patient is incidental — we
# throw the data away and only keep the headers.
WARMUP_PATIENT_ID = "1133"


class ApiSession:
    """Owns the browser session and hands out ready-to-use API clients."""

    def __init__(self, warmup_patient_id: str = WARMUP_PATIENT_ID):
        self._warmup_patient_id = warmup_patient_id
        self._capture: HeaderCapture | None = None
        self._page = None
        self._lock = asyncio.Lock()
        self._tokens = TokenManager(self._acquire)
        self._last_error: str | None = None
        # Browserless session. Primary when config.USE_HTTP_LOGIN is on.
        self._http: AthenaHttpSession | None = None
        self._http_headers: dict | None = None

    # -- browser side ---------------------------------------------------

    async def _ensure_logged_in(self, on_step) -> None:
        page = await browser_pool.ensure_page()
        if self._capture is None or self._page is not page:
            self._capture = HeaderCapture(page)
            self._page = page

        if await browser_pool.is_logged_in(page):
            await on_step("Reusing existing authenticated session")
            return

        # Clear cookies FIRST. A stale session cookie makes athenahealth
        # serve its re-authentication page instead of the normal sign-in
        # form, and that page does not match what login() drives.
        await browser_pool.clear_session_cookies()
        await on_step(f"Opening {config.ATHENA_LOGIN_URL}")
        await page.goto(config.ATHENA_LOGIN_URL, wait_until="domcontentloaded")
        browser_pool.record_login(await login(
            page,
            username=config.ATHENA_USERNAME,
            password=config.ATHENA_PASSWORD,
            totp_secret=config.ATHENA_TOTP_SECRET,
            on_step=on_step,
        ))

    def _captured_token_is_fresh(self, min_expiry: float = 0.0) -> bool:
        """Is the token the page last sent us good enough to use?

        `min_expiry` is what makes this correct during renewal. Without it
        the question is only "has it expired yet?", and the token we are
        trying to REPLACE answers yes — it is the one currently in use, so
        of course it has life left.

        That made _provoke_graphql return at its first step and never
        escalate to the path that actually provokes a new token. The loop
        then re-attempted every 30s, each time re-reading the same token,
        until the app happened to issue a request of its own. Observed as
        the token bottoming out at ~8s remaining every cycle no matter what
        the renewal margin was.

        Passing the current expiry turns the question into "is there
        anything NEWER here?", which is what the caller actually wants.
        """
        headers = (self._capture.headers if self._capture else None) or {}
        token = headers.get("authorization", "")
        expiry = decode_expiry(token.replace("Bearer ", ""))
        if not expiry or (expiry - time.time()) <= MIN_TOKEN_LIFE_S:
            return False
        return expiry > min_expiry

    async def _provoke_graphql(self, on_step, min_expiry: float = 0.0) -> None:
        """Ensure we hold a currently-valid token, as cheaply as possible.

        Ordered by cost, because getting this wrong is expensive: an
        earlier version reloaded unconditionally and that alone accounted
        for 36 of a 54-second request — reloading the Care Management pane
        is the single heaviest page in the app.

          1. Token the page already gave us is still good -> do nothing.
          2. Reload a LIGHT page to make the app re-auth -> a few seconds.
          3. Open Care Management (first run only) -> tens of seconds.
        """
        if self._captured_token_is_fresh(min_expiry):
            return

        if self._capture and self._capture.headers:
            await self._capture.refresh()
            if self._captured_token_is_fresh(min_expiry):
                return

        await search_patient(self._page, self._warmup_patient_id, on_step)
        await open_care_management_pane(self._page, on_step)

    # -- browserless path -----------------------------------------------

    def _http_session(self) -> AthenaHttpSession:
        if self._http is None:
            self._http = AthenaHttpSession(
                login_url=config.ATHENA_LOGIN_URL,
                username=config.ATHENA_USERNAME,
                password=config.ATHENA_PASSWORD,
                totp_secret=config.ATHENA_TOTP_SECRET,
                environment=config.ATHENA_ENVIRONMENT,
                practice=config.ATHENA_PRACTICE,
                department=config.ATHENA_DEPARTMENT,
            )
        return self._http

    async def _acquire_http(self, on_step) -> tuple[str, str] | None:
        """Log in and mint a token over plain HTTP. No browser involved.

        Returns None if it cannot be used, so the caller falls back to the
        browser rather than losing the service to an approach that is still
        young. Everything here is blocking urllib, so it runs off the event
        loop.

        Re-login is the point. On the browser path that meant driving a
        form whose shape changes when a session goes stale, and it is what
        took production down twice. Here it is the same two API calls as
        the first login, every time.
        """
        if not config.USE_HTTP_LOGIN:
            return None

        loop = asyncio.get_running_loop()
        session = self._http_session()

        def work():
            # A session with idle budget left can just mint. Otherwise log
            # in first — athenahealth publishes the idle timeout, so this
            # is a real check rather than a guess.
            if session.seconds_until_session_expiry < SESSION_RENEW_MARGIN_S:
                session.login()
                return session.mint_token(), True
            try:
                return session.mint_token(), False
            except LoginFailed:
                session.login()          # session died early; start over
                return session.mint_token(), True

        try:
            (token, ttl), logged_in = await loop.run_in_executor(None, work)
        except Exception as exc:
            await on_step(f"HTTP login unavailable, falling back to browser: {exc}")
            return None

        await on_step(f"Minted a token over HTTP ({ttl}s)"
                      + (" after a fresh login" if logged_in else ""))
        self._http_headers = session.api_headers(token)
        return f"Bearer {token}", config.ATHENA_PRACTICE

    async def _mint(self, on_step) -> tuple[str, str] | None:
        """Fast path: ask the app's own token endpoint for a fresh JWT.

        This is what renewal should almost always do. The alternative —
        reloading a page and hoping to intercept a request the app makes
        on its own — costs 60-200s on a small instance and depends on the
        page choosing to do something. This costs ~0.7s and depends on
        nothing.

        Returns None rather than raising when it can't be used, so the
        caller falls back to the slow path instead of failing outright.
        The endpoint is undocumented, so it must never be the only way in.
        """
        if not self._page or not (self._capture and self._capture.headers):
            return None                 # no session yet — bootstrap first
        try:
            token, ttl = await mint_token(self._page)
        except Exception as exc:
            await on_step(f"Token endpoint unavailable, falling back: {exc}")
            return None
        headers = dict(self._capture.headers)
        headers["authorization"] = f"Bearer {token}"
        self._capture._headers = headers          # keep the capture current
        await on_step(f"Minted a token ({ttl}s)")
        return f"Bearer {token}", headers.get("x-athena-context", "")

    async def _attempt(self, on_step, min_expiry: float = 0.0) -> tuple[str, str]:
        """One acquisition attempt. Raises if it could only produce a token
        we already hold.

        The freshness check at the end is load-bearing. HeaderCapture's
        refresh() deliberately returns the OLD headers when a reload times
        out — losing a working token would be worse — but that means this
        can otherwise hand back the very token it was asked to replace.

        The renewal loop then stored it, _expires_at never moved,
        needs_renewal stayed true, and it tried again 30s later, reloading
        a heavy page each time until the app happened to emit a request of
        its own. Observed as the token bottoming out at 3-10s remaining on
        every cycle regardless of the margin — raising the margin just made
        the loop start spinning sooner.

        Raising here instead lets _acquire's second attempt throw the page
        away and do a real re-login, which actually produces a new token.
        """
        # Browserless first. It is faster and has no page to go wrong.
        over_http = await self._acquire_http(on_step)
        if over_http:
            return over_http

        await self._ensure_logged_in(on_step)

        minted = await self._mint(on_step)
        if minted:
            return minted

        await self._provoke_graphql(on_step, min_expiry)
        headers = await self._capture.wait_for_headers()
        if not self._captured_token_is_fresh(min_expiry):
            raise RuntimeError(
                "Acquisition produced no fresher token than the one held")
        return headers["authorization"], headers.get("x-athena-context", "")

    async def _acquire(self, min_expiry: float = 0.0) -> tuple[str, str]:
        """TokenManager calls this when it needs credentials.

        Serialised by TokenManager's own single-flight lock, so concurrent
        requests never trigger two logins — which matters because two
        logins in the same 30-second window would present the same TOTP
        code and the second would be rejected.

        Two attempts, and the escalation between them is the point. The
        shared page can end up in a state that `is_logged_in` still calls
        healthy — it checks the search box is visible, which does not mean
        the search *dropdown* works — and the first attempt then dies with
        something like "Could not locate 'Patient ID' option".

        Retrying that as-is is useless: the renewal loop would drive the
        same wedged page every 30s forever, which is exactly how this
        failed in testing (hasHeaders true, hasToken false, same error
        pinned in /health indefinitely). So a failure throws the page away
        and forces a genuine re-login instead of retrying into it.
        """
        async def on_step(message: str) -> None:
            pass

        try:
            token = await self._attempt(on_step, min_expiry)
            self._last_error = None
            return token
        except Exception as first:
            self._last_error = f"{type(first).__name__}: {first} (rebuilding session)"

        await browser_pool.reset_page()
        self._capture = None          # belongs to the discarded page
        self._page = None
        try:
            token = await self._attempt(on_step, min_expiry)
            self._last_error = None
            return token
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise

    # -- API side -------------------------------------------------------

    async def client(self, department_id: str | None = None) -> CareManagementClient:
        """A client with currently-valid credentials.

        Every captured header is replayed, not just the token — an earlier
        version sent only `authorization` + `x-athena-context` and failed
        inside the resolver with "Unspecified Athena environment".
        """
        # current(), not get(): a request must never trigger a login. See
        # TokenManager.current().
        token, _ = self._tokens.current()
        # Headers come from whichever path produced the token.
        headers = dict(getattr(self, "_http_headers", None)
                       or (self._capture.headers if self._capture else None) or {})
        if not headers:
            raise RuntimeError("No API headers available yet")
        # The non-auth headers come from the capture (they never change);
        # the token comes from TokenManager, which is the only thing that
        # knows whether it is current.
        headers["authorization"] = token
        client = CareManagementClient(headers)
        return client.for_department(department_id) if department_id else client

    async def invalidate(self) -> None:
        """After a 401/403 — forces fresh credentials on the next call."""
        await self._tokens.invalidate()

    async def warm_up(self, on_step=None) -> None:
        """Log in at startup so no request ever waits on it.

        Deliberately just asks TokenManager for a token rather than
        driving login + _provoke_graphql itself. An earlier version did
        duplicate those two steps here, which meant startup was the ONE
        path that skipped the retry-and-rebuild escalation in _acquire —
        so a single flaky navigation (a real, observed failure) left the
        session dead until the renewal loop happened to fix it.

        One path in, one place that knows how to recover.
        """
        async with self._lock:
            await self._tokens.get()

    async def run_renewal_loop(self, interval_s: int = 30) -> None:
        await self._tokens.run_renewal_loop(interval_s)

    def status(self) -> dict:
        token_status = self._tokens.status()
        headers = self._capture.headers if self._capture else None
        return {
            **token_status,
            "loginMode": "http" if self._http_headers else "browser",
            "sessionIdleSecondsLeft": (
                round(self._http.seconds_until_session_expiry) if self._http else None),
            "hasHeaders": bool(headers or self._http_headers),
            # Read from whichever path is live. These came only from the
            # browser capture before, so /health reported null for both
            # while the HTTP path was working perfectly — a diagnostic
            # surface that lies about a healthy service is worse than one
            # that says nothing.
            "context": ((self._http_headers or headers or {})
                        .get("x-athena-context")),
            "environment": ((self._http_headers or headers or {})
                            .get("x-athena-environment")),
            "sessionError": self._last_error,
        }


session = ApiSession()
