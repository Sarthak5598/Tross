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

GRAPHQL_URL_PART = "caremanagement-api/graphql"

WANTED_HEADERS = (
    "authorization",
    "x-athena-context",
    "x-athena-environment",
    "x-athena-department",
)

# How long to wait for the app to make a GraphQL request we can read.
CAPTURE_TIMEOUT_S = 20


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


async def acquire_via_browser(page, capture: HeaderCapture) -> tuple[str, str]:
    """Shape token_manager expects: (token, context).

    The remaining headers travel with them — see graphql.CareManagementClient,
    which replays every captured header rather than a curated few.
    """
    headers = await capture.wait_for_headers()
    return headers["authorization"], headers.get("x-athena-context", "")
