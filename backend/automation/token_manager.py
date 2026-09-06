"""Keeps a valid Care Management API token available at all times.

Why this shape:

The token lives about **5 minutes** (measured: `exp - iat`), while a login
costs 20-30 seconds and requires MFA. So the token cannot be fetched per
request, and it cannot be fetched lazily either — whichever caller arrived
first would eat the login, and again every five minutes. For programmatic
callers (agents) that is the wrong shape entirely.

Instead:

  * **Eager** — acquire at startup so no request ever waits on a login.
  * **Proactive** — renew before expiry, not after a request fails.
  * **Single-flight** — concurrent callers needing a token trigger exactly
    ONE acquisition and all wait on it. This is not just efficiency: TOTP
    codes are single-use within their 30-second window, so two logins
    racing would present the same code and the second would be rejected.

Renewal does NOT re-login. The browser session outlives the token and the
app silently renews it, so we read a fresh token off the live session.
A full re-login is the fallback for when the session itself has died.

(`offline_access` is in the OAuth scope, but the refresh token isn't in
browser storage — `okta-token-storage` was empty — so refreshing over
plain HTTP isn't available to us. Reading from the live session is.)
"""

import asyncio
import base64
import json
import time

# Renew with this much life left.
#
# The normal path costs ~0.7s: the app exposes its own token endpoint and
# ApiSession._mint() calls it directly (see token_source.mint_token). At
# that price the margin barely matters.
#
# It is sized for the FALLBACK. If that endpoint ever changes or fails,
# acquisition reverts to reloading a page and intercepting a request the
# app makes on its own, which was measured at 60s, 145s and 165s on the
# deployed instance. 220s of a 300s token means even the slowest observed
# fallback completes with time in hand.
#
# Before the token endpoint was found this margin was doing all the work,
# and it could not: acquisition expanded to fill whatever it was set to,
# bottoming the token out at ~8s remaining whether it was 90, 150 or 220.
RENEW_MARGIN_S = 220

# Treat a token as unusable below this. Distinct from the margin above so a
# request arriving mid-renewal can still use a token that has enough left.
MIN_USABLE_S = 20

# Ceiling on the retry backoff. Long enough to stop hammering a login that
# is not going to succeed, short enough that recovery is automatic once
# whatever was wrong clears.
MAX_RETRY_INTERVAL_S = 300


def decode_expiry(token: str) -> float | None:
    """Read `exp` out of the JWT without verifying it — we're scheduling
    renewal, not validating trust. Returns None if it isn't a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


class SessionUnavailable(Exception):
    """No usable token, and the request path will not wait for one."""


class TokenManager:
    """Holds the current token and serialises acquisition.

    `acquire` is injected rather than imported so this stays testable
    without a browser, and so the browser dependency lives in one place.
    """

    def __init__(self, acquire):
        # acquire() -> (token: str, context: str)
        self._acquire = acquire
        self._token: str | None = None
        self._context: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self._expires_at - time.time())

    def _usable(self) -> bool:
        return bool(self._token) and self.seconds_remaining > MIN_USABLE_S

    def needs_renewal(self) -> bool:
        return not self._token or self.seconds_remaining < RENEW_MARGIN_S

    def current(self) -> tuple[str, str]:
        """The token we hold, or raise. Never acquires, never blocks.

        This is what the REQUEST path uses. Acquisition belongs to the
        background worker, and letting a request trigger it was a genuine
        design fault: when a login started failing, every incoming request
        queued behind a 120s page action, retried, and hung for minutes
        before timing out. A caller would far rather have 503 in
        milliseconds than a connection that never answers.
        """
        if not self._usable():
            detail = f" ({self._last_error})" if self._last_error else ""
            raise SessionUnavailable(
                f"No valid athenahealth session right now{detail}")
        return self._token, self._context

    async def get(self) -> tuple[str, str]:
        """Current token and context, acquiring one if needed.

        For the STARTUP and renewal paths. Request handling uses current().

        The double check around the lock is the single-flight part: many
        callers may find the token missing, but only the first through the
        lock actually acquires — the rest find it already there.
        """
        if self._usable():
            return self._token, self._context

        async with self._lock:
            if self._usable():
                return self._token, self._context

            token, context = await self._acquire()
            expiry = decode_expiry(token)
            self._token = token
            self._context = context
            # If it isn't a JWT we can't know the real expiry, so assume the
            # observed 5 minutes and renew conservatively.
            self._expires_at = expiry if expiry else time.time() + 300
            self._last_error = None
            return self._token, self._context

    async def invalidate(self) -> None:
        """Drop the current token — call after a 401/403, where we know the
        token is genuinely dead.

        NOT for proactive renewal: see _renew(), which must not create a
        window where we hold nothing.
        """
        async with self._lock:
            self._token = None
            self._expires_at = 0.0

    async def _renew(self) -> None:
        """Replace the token while the current one keeps serving.

        The old token stays in place for the whole acquisition and is
        swapped out only once a replacement is in hand. That ordering is
        the entire point of this method.

        An earlier version renewed by calling invalidate() and then get(),
        which threw away a token still good for ~90 seconds and only then
        started fetching. Acquisition can take up to ~40s (it may have to
        reload a page to make the app re-auth), so the service held NO
        token for that whole stretch and any request arriving in it paid
        the acquisition cost — measured at 18.4s against 7.4s normally.
        Visible on /health as secondsRemaining sitting at 0 for ~40s every
        renewal cycle.

        The lock is taken only for the swap, not for the acquisition, so
        readers are never blocked behind a slow network call.
        """
        # Tell acquisition what "newer" means, so it escalates instead of
        # handing back the token we are trying to replace.
        token, context = await self._acquire(self._expires_at)
        expiry = decode_expiry(token)
        # Never accept a "replacement" that expires no later than what we
        # already hold. Storing it would leave needs_renewal true forever
        # and turn this loop into a page-reload spinner.
        if expiry and expiry <= self._expires_at:
            raise RuntimeError(
                f"Acquired token is not newer "
                f"({expiry - self._expires_at:.0f}s difference)")
        async with self._lock:
            self._token = token
            self._context = context
            self._expires_at = expiry if expiry else time.time() + 300
            self._last_error = None

    async def run_renewal_loop(self, interval_s: int = 30) -> None:
        """Background task: keep a valid token ready so no request ever
        pays for acquisition.

        Failures are recorded and retried rather than killing the loop — a
        transient athenahealth outage shouldn't permanently stop renewal.

        Retries back off. A failing renewal escalates to a full re-login,
        and hammering that every 30 seconds is both useless against a
        problem that isn't transient and a good way to get an account rate
        limited or locked — which turns a recoverable outage into one that
        needs a human.
        """
        delay = interval_s
        while True:
            try:
                if self.needs_renewal():
                    await self._renew()
                self._last_error = None
                delay = interval_s
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                delay = min(delay * 2, MAX_RETRY_INTERVAL_S)
            await asyncio.sleep(delay)

    def status(self) -> dict:
        """For /health — a broken auth should be visible here rather than
        showing up as unexplained 500s on requests."""
        return {
            "hasToken": bool(self._token),
            "secondsRemaining": round(self.seconds_remaining),
            "needsRenewal": self.needs_renewal(),
            "lastError": self._last_error,
        }
