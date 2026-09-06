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

# Renew with this much life left. Generous relative to a 5-minute token:
# a request that starts just before expiry must still be able to finish.
RENEW_MARGIN_S = 90

# Treat a token as unusable below this. Distinct from the margin above so a
# request arriving mid-renewal can still use a token that has enough left.
MIN_USABLE_S = 20


def decode_expiry(token: str) -> float | None:
    """Read `exp` out of the JWT without verifying it — we're scheduling
    renewal, not validating trust. Returns None if it isn't a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


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

    async def get(self) -> tuple[str, str]:
        """Current token and context, acquiring one if needed.

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
        """Drop the current token — call after a 401/403 so the next get()
        fetches a fresh one instead of retrying a dead token."""
        async with self._lock:
            self._token = None
            self._expires_at = 0.0

    async def run_renewal_loop(self, interval_s: int = 30) -> None:
        """Background task: keep a valid token ready so no request ever
        pays for acquisition. Failures are recorded and retried rather than
        killing the loop — a transient athenahealth outage shouldn't
        permanently stop renewal."""
        while True:
            try:
                if self.needs_renewal():
                    await self.invalidate()
                    await self.get()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(interval_s)

    def status(self) -> dict:
        """For /health — a broken auth should be visible here rather than
        showing up as unexplained 500s on requests."""
        return {
            "hasToken": bool(self._token),
            "secondsRemaining": round(self.seconds_remaining),
            "needsRenewal": self.needs_renewal(),
            "lastError": self._last_error,
        }
