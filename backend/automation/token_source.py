"""Obtains Care Management API credentials from a live browser session.

This is the bridge between the two worlds: Playwright exists solely to
hold a logged-in session, and everything downstream is plain HTTP.

Nobody supplies these headers by hand. The app itself sends them on every
GraphQL request it makes, so we let it make one and intercept the
headers off it. Four matter:

    authorization         Bearer <jwt>, ~5 minute lifetime
    x-athena-context      practice id
    x-athena-environment  e.g. "preview@nva"
    x-athena-department   numeric department id

Refresh does NOT require logging in again. The browser session long
outlives the 5-minute token and the app silently renews it, so a fresh
token is obtained by prompting one more request and reading it. A full
re-login is only needed if the session itself has died.
"""

import asyncio
import json
import re
from urllib.parse import quote

GRAPHQL_URL_PART = "caremanagement-api/graphql"

# The app's own token endpoint, found by watching which responses contain a
# JWT while the Treatment Plan pane loads:
#
#   GET /<practice>/<ctx>/ax/jwt/get_jwt?scopes[]=...  ->  {jwt, expires_in}
#
# Same-origin and authenticated by the session cookie, so calling it from
# inside the page needs no credentials of our own. It is the difference
# between renewal costing 0.7s and renewal costing ~200s: the alternative
# is reloading a page and hoping to intercept a request the app makes on
# its own, which on a small instance takes most of the token's life.
JWT_ENDPOINT = "/ax/jwt/get_jwt"

# Exactly the scopes on the token the Care Management API accepts, read off
# a real captured token's `scp` claim. Asking for a different set gets a
# valid JWT that the resolver then rejects.
CARE_MANAGEMENT_SCOPES = (
    "athena/user/CareManagement.Api.*",
    "user/Condition.read",
    "user/Observation.read",
)

# Matches the /<practice>/<context>/ prefix every app URL carries, e.g.
# https://preview.athenahealth.com/32817/15/globalframeset.esp
_APP_PREFIX = re.compile(r"^(https://[^/]+/\d+/\d+)")

WANTED_HEADERS = (
    "authorization",
    "x-athena-context",
    "x-athena-environment",
    "x-athena-department",
)

# How long to wait for the app to make a GraphQL request we can read.
# How long to wait for the app to issue a GraphQL request we can read.
# Only reached on the fallback path now that tokens are minted directly,
# and that path is the slow one, so it gets room.
CAPTURE_TIMEOUT_S = 90


class HeaderCapture:
    """Watches a page and keeps the most recent GraphQL request headers.

    Attached once for the life of the page. Because the app re-requests
    regularly, the stored headers naturally track the current token — so
    refreshing is usually just reading this again, with no page action at
    all.
    """

    def __init__(self, page):
        self._page = page
        self._headers: dict | None = None
        self._event = asyncio.Event()
        page.on("request", lambda r: asyncio.ensure_future(self._on_request(r)))

    async def _on_request(self, request) -> None:
        if GRAPHQL_URL_PART not in request.url:
            return
        try:
            # all_headers() rather than the sync `.headers` property: the
            # latter can omit headers added later in the request lifecycle,
            # which is how an earlier capture attempt came back empty.
            headers = await request.all_headers()
        except Exception:
            return
        if not headers.get("authorization"):
            return
        self._headers = {k: v for k, v in headers.items() if k.lower() in WANTED_HEADERS}
        self._event.set()

    @property
    def headers(self) -> dict | None:
        return self._headers

    async def wait_for_headers(self, timeout_s: int = CAPTURE_TIMEOUT_S) -> dict:
        if self._headers:
            return self._headers
        await asyncio.wait_for(self._event.wait(), timeout=timeout_s)
        return self._headers

    async def refresh(self, timeout_s: int = CAPTURE_TIMEOUT_S) -> dict:
        """Prompt the app to issue a GraphQL request so we see a fresh token.

        Deliberately keeps the OLD headers while waiting: if the app
        happens to issue a call on its own, we take that and skip the wait
        entirely. Reload is the fallback, and `domcontentloaded` matters —
        this app's `load` event often never fires (TROUBLESHOOTING.md #9).
        """
        self._event.clear()
        try:
            await self._page.reload(wait_until="domcontentloaded")
        except Exception:
            pass          # a failed reload shouldn't lose a good token
        try:
            return await self.wait_for_headers(timeout_s)
        except asyncio.TimeoutError:
            return self._headers or {}


def _app_prefix(page) -> str | None:
    """The origin + /<practice>/<context>/ prefix, from any live frame.

    Derived rather than hardcoded — the practice id is part of the URL and
    differs per environment.
    """
    for url in [page.url] + [f.url for f in page.frames]:
        found = _APP_PREFIX.match(url or "")
        if found:
            return found.group(1)
    return None


async def mint_token(page, scopes=CARE_MANAGEMENT_SCOPES) -> tuple[str, int]:
    """Ask the app's token endpoint for a fresh JWT. Returns (jwt, ttl).

    Runs the fetch inside the page so the session cookie, origin and any
    CSRF handling come for free. Raises if the endpoint is unreachable or
    returns something without a JWT, so the caller can fall back to the
    slow provoke-and-intercept path.
    """
    prefix = _app_prefix(page)
    if not prefix:
        raise RuntimeError("No app URL to derive the JWT endpoint from")

    query = "&".join("scopes[]=" + quote(s, safe="") for s in scopes)
    url = f"{prefix}{JWT_ENDPOINT}?{query}"

    result = await page.evaluate("""async (u) => {
        const r = await fetch(u, {credentials: 'include'});
        return {status: r.status, body: (await r.text()).slice(0, 8000)};
    }""", url)

    if result["status"] != 200:
        raise RuntimeError(f"get_jwt returned HTTP {result['status']}")
    try:
        payload = json.loads(result["body"])
        return payload["jwt"], int(payload.get("expires_in") or 0)
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"get_jwt returned no jwt: {result['body'][:120]}") from exc


async def acquire_via_browser(page, capture: HeaderCapture) -> tuple[str, str]:
    """Shape token_manager expects: (token, context).

    The remaining headers travel with them — see graphql.CareManagementClient,
    which replays every captured header rather than a curated few.
    """
    headers = await capture.wait_for_headers()
    return headers["authorization"], headers.get("x-athena-context", "")
