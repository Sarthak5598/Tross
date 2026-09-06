"""Direct client for athenahealth's Care Management GraphQL API.

This is the same endpoint the Treatment Plan UI drives itself — we read it
directly instead of scraping the rendered page. That removes essentially
every failure mode the DOM path has (stale sessions, invisible elements,
render timing, silent empty extractions) and turns six sequential goal
expansions into parallel requests.

Three headers matter, all captured from a logged-in browser session:

  authorization        Bearer <jwt>   — lives only ~5 minutes
  x-athena-context     practice id    — e.g. "32817"
  x-athena-environment e.g. "preview@nva"
  x-athena-department  numeric id     — e.g. "4" for SH OH - Shaker

`x-athena-environment` is easy to miss and not optional: without it the
request authenticates and then fails inside the resolver with
"Unspecified Athena environment".

**Department is a per-request header, not session state.** That is the
key difference from the browser path, where switching department moved
every open tab and forced everything to serialise. Here one token serves
all eight departments — see `for_department()`.

Operation documents live in gql_queries.py, transcribed from the app's
own requests rather than reconstructed. Note `patientId` is `ID!`, not
`String!` — declaring it as String is rejected by schema validation.

Nothing here is documented or stable by contract: it's an internal API and
athenahealth may change it without notice. The mitigation is that the
JSON is far more stable than the CSS class names we were depending on
before, and a shape change here fails loudly rather than silently
returning an empty list.
"""

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request

from automation.gql_queries import QUERIES

DEFAULT_ENDPOINT = "https://caremanagement.preview.api.athena.io/caremanagement-api/graphql"

# Concurrency limit, set from measurement rather than guesswork. Per-call
# latency across a 34-call fan-out, 3 runs each (see docs/ENDPOINTS.md):
#
#     conc    wall    median per call
#        4   19.4s      1.98s
#        8   11.0s      2.04s
#       16    9.2s      2.06s     <- chosen
#       32    9.3s      5.05s
#
# Two things to read off that. Median per-call time is FLAT from 4 to 16,
# with zero non-200s across every run — so athenahealth is not throttling
# us, and an earlier comment here claiming it was is simply wrong; it was
# reading a pattern into single unrepeated samples.
#
# Second, wall clock stops improving at 16 because by then it equals the
# SLOWEST SINGLE CALL (~9s, a fat task-schedules response). Past that the
# fan-out is no longer the bottleneck and more workers cannot help — the
# rise at 32 is our own thread pool queueing, not their server.
MAX_CONCURRENT_CALLS = int(os.environ.get("GQL_MAX_CONCURRENCY", "16"))
_POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS,
                           thread_name_prefix="gql")
_LIMIT = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

# Wide enough to cover every record; the UI itself sends a range like this.
HISTORY_START = "2018-01-01T05:00:00.000Z"


class GraphQLError(Exception):
    """The API rejected the request or returned GraphQL-level errors."""


class TokenExpired(GraphQLError):
    """401/403 — the caller should refresh the token and retry once."""


# Every phrasing seen from upstream for "this patient isn't here".
NOT_FOUND_MARKERS = (
    "CODE: 404",
    "Not Found",
    "does not exist",
)


class PatientNotFound(GraphQLError):
    """Upstream said the patient/department pair doesn't resolve."""


class InvalidRequest(GraphQLError):
    """Upstream rejected our arguments — caller input problem, not a bug."""


def _summarise(errors: list) -> str:
    """A readable one-liner from a GraphQL errors array.

    Worth doing deliberately: these payloads embed a full Node stacktrace,
    and returning it raw both leaks internals to the caller and buries the
    one line that actually says what went wrong.
    """
    parts = []
    for err in errors:
        message = str(err.get("message", ""))
        # The useful text is nested inside "Unexpected error value: {...}"
        match = re.search(r'MDP_ERROR:\s*"([^"]+)"', message)
        if not match:
            match = re.search(r'message:\s*"([^"]+)"', message)
        parts.append(match.group(1) if match else message[:200])
    return "; ".join(parts) or "unknown GraphQL error"


class CareManagementClient:
    """Runs operations against the Care Management API.

    Deliberately holds no session state beyond the token and context — that
    is what lets requests run concurrently, unlike the browser path where
    everything contends over one page.
    """

    # Headers that belong to the browser's own transport and must not be
    # replayed — they'd conflict with what urllib sets, or describe a body
    # we're re-encoding ourselves.
    SKIP_HEADERS = {
        "host", "content-length", "connection", "accept-encoding",
        "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest",
    }

    def __init__(self, headers: dict, endpoint: str = DEFAULT_ENDPOINT):
        """`headers` is every header from a real GraphQL request made by
        the app, captured verbatim.

        Deliberately NOT a curated list. An earlier version passed only
        `authorization` + `x-athena-context`, which authenticated fine and
        then failed inside the resolver with "Unspecified Athena
        environment" — the app sends context we hadn't thought to look
        for. Replaying what it actually sends avoids having to guess which
        headers carry meaning.
        """
        self.headers = {
            k.lower(): v for k, v in headers.items()
            if k.lower() not in self.SKIP_HEADERS
        }
        self.endpoint = endpoint

    @property
    def token(self) -> str | None:
        return self.headers.get("authorization")

    @property
    def department(self) -> str | None:
        return self.headers.get("x-athena-department")

    def for_department(self, department_id: str | int) -> "CareManagementClient":
        """A client pointed at a different department, same token.

        Cheap by design: department rides in a header, so switching costs
        nothing and two departments can be queried concurrently. On the
        browser path this was impossible — department was shared session
        state, so changing it in one place moved everything (see
        TROUBLESHOOTING.md #24).
        """
        headers = dict(self.headers)
        headers["x-athena-department"] = str(department_id)
        return CareManagementClient(headers, self.endpoint)

    def _post(self, operation: str, variables: dict) -> dict:
        payload = json.dumps({
            "operationName": operation,
            "variables": variables,
            "query": QUERIES[operation],
        }).encode()

        req = urllib.request.Request(self.endpoint, data=payload, method="POST")
        for name, value in self.headers.items():
            req.add_header(name, value)
        req.add_header("content-type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise TokenExpired(f"{operation}: HTTP {exc.code} — {detail}") from exc
            raise GraphQLError(f"{operation}: HTTP {exc.code} — {detail}") from exc

        # A 200 with an `errors` array is still a failure. Surfacing it as
        # an exception matters: the DOM path's habit of quietly returning
        # empty on trouble is exactly what hid three regressions.
        if body.get("errors"):
            raw = json.dumps(body["errors"])
            detail = _summarise(body["errors"])
            # athenahealth reports a missing patient as a 200 carrying the
            # real status in the errors array, so it has to be dug out —
            # otherwise a plain bad id surfaces as our 500, which reads as
            # "this service is broken".
            #
            # There is more than one wording. An unknown id gives
            # `CODE: 404`, but an id that exists outside the active
            # department gives only the prose "The specified patient does
            # not exist in that department" with no code at all. Matching
            # just the code missed that entirely and returned 500.
            if any(m in raw for m in NOT_FOUND_MARKERS):
                raise PatientNotFound(detail)
            if "must be integer" in raw or "request/params" in raw:
                raise InvalidRequest(detail)
            raise GraphQLError(f"{operation}: {detail}")

        return body.get("data") or {}

    async def run(self, operation: str, **variables) -> dict:
        """Run the blocking call off the event loop, on a pool sized for
        network waits rather than CPU — see _POOL."""
        # Semaphore as well as pool size: the pool bounds threads, this
        # bounds in-flight requests to athenahealth, which is the thing
        # that actually degrades when overloaded.
        async with _LIMIT:
            return await asyncio.get_running_loop().run_in_executor(
                _POOL, self._post, operation, variables)

    async def run_many(self, calls: list[tuple[str, dict]]) -> list:
        """Fire independent operations together. This is where the time is
        won: six goals' worth of detail costs one round trip, not six."""
        return await asyncio.gather(
            *(self.run(op, **vars_) for op, vars_ in calls),
            return_exceptions=True,
        )
