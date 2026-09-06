"""Log in to athenahealth over plain HTTP. No browser.

The browser existed for one reason: to hold a logged-in session. Every
outage this project has had came from driving a web form to establish
that session — a disabled field during the MFA transition, a
re-authentication page served because of a stale cookie, a frame that took
longer than a timeout to appear. None of those failure modes exist here,
because there is no page.

The chain, all of it verified end to end:

    POST identity/api/v1/authn                 username + password
    POST .../factors/{id}/verify               TOTP        -> sessionToken
    GET  <athena>                              -> Okta authorize URL
    GET  authorize?...&sessionToken=...        -> code (form_post)
    POST oidc.esp                              code        -> session cookies
    GET  <prefix>/ax/jwt/get_jwt?scopes[]=...              -> bearer token

Two details that are easy to get wrong:

  * The authorize URL must be the one athenahealth generates, not one we
    build. It carries a `state` containing athena's own CSRF token,
    practice and department, which is validated on the way back.
  * That URL arrives with `prompt=login`, which makes Okta ignore a
    sessionToken and render the sign-in page instead. It has to be
    removed.

Session lifetime is not guessed: athenahealth sets TIMEOUT_UNENCRYPTED to
the idle timeout in seconds (1800 — thirty minutes), so we can re-login
ahead of expiry rather than discovering it through a failed request.
"""

import html as htmlmod
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pyotp

OKTA_BASE = "https://identity.athenahealth.com"
JWT_ENDPOINT = "/ax/jwt/get_jwt"

# Exactly the scopes on a token the Care Management API accepts, read off a
# real captured token's `scp` claim. A different set mints a valid JWT that
# the resolver then rejects.
CARE_MANAGEMENT_SCOPES = (
    "athena/user/CareManagement.Api.*",
    "user/Condition.read",
    "user/Observation.read",
)

# The app carries its practice and context in the URL: /<practice>/<ctx>/.
# Login lands on /1/1/login/oidc.esp, which is NOT that prefix — it has to
# be discovered by following into the app itself.
APP_PREFIX = re.compile(r"^(https://[^/]+/(\d+)/(\d+))/")

# Fallback if athenahealth stops telling us. Thirty minutes is what it
# currently reports.
DEFAULT_SESSION_TIMEOUT_S = 1800

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class LoginFailed(Exception):
    """Authentication did not complete. The message says which step."""


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Records the redirect chain, because the authorize URL we need is one
    of the hops rather than the final destination."""

    def __init__(self):
        self.chain = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# A TOTP code is valid for one 30-second window and Okta rejects a reused
# one. Two logins close together — a retry right after a failure, or a
# re-login shortly after startup — would otherwise present the same code
# and the second would fail for a reason that looks like bad credentials.
TOTP_PERIOD_S = 30


class AthenaHttpSession:
    """A logged-in athenahealth session held as cookies, not as a browser."""

    def __init__(self, login_url, username, password, totp_secret,
                 environment, practice, department=None, timeout=60):
        self._login_url = login_url
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._environment = environment
        self._practice = str(practice)
        self._department = department

        self._timeout = timeout
        self._jar = None
        self._opener = None
        self._prefix = None
        self._logged_in_at = 0.0
        self._session_timeout_s = DEFAULT_SESSION_TIMEOUT_S
        self._last_totp = None

    # -- plumbing -------------------------------------------------------

    def _new_opener(self):
        self._jar = http.cookiejar.CookieJar()
        redirects = _Redirects()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar), redirects)
        opener.addheaders = [("User-Agent", BROWSER_UA),
                             ("Accept", "text/html,application/xhtml+xml")]
        return opener, redirects

    def _post_json(self, url, payload):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise LoginFailed(f"{url.rsplit('/', 1)[-1]}: HTTP {exc.code} {detail}") from exc

    # -- the flow -------------------------------------------------------

    def _fresh_totp(self):
        """A code we have not already spent.

        Okta accepts a given code once. If the previous login used the code
        for this window, wait for the next one rather than sending it again
        — a rejected reuse is indistinguishable from bad credentials in the
        response, which makes it miserable to diagnose.
        """
        code = pyotp.TOTP(self._totp_secret).now()
        if code == self._last_totp:
            wait = TOTP_PERIOD_S - (time.time() % TOTP_PERIOD_S) + 1
            time.sleep(wait)
            code = pyotp.TOTP(self._totp_secret).now()
        self._last_totp = code
        return code

    def _session_token(self):
        """Okta authn + TOTP. Returns a one-shot session token."""
        body = self._post_json(f"{OKTA_BASE}/api/v1/authn",
                               {"username": self._username,
                                "password": self._password})
        if body.get("sessionToken"):
            return body["sessionToken"]          # MFA not required

        if body.get("status") != "MFA_REQUIRED":
            raise LoginFailed(f"unexpected authn status {body.get('status')!r}")

        factors = (body.get("_embedded") or {}).get("factors") or []
        totp = next((f for f in factors
                     if f.get("factorType") in ("token:software:totp", "token:hardware")), None)
        if not totp:
            kinds = [f.get("factorType") for f in factors]
            raise LoginFailed(f"no TOTP factor offered, only {kinds}")

        verify = ((totp.get("_links") or {}).get("verify") or {}).get("href") \
            or f"{OKTA_BASE}/api/v1/authn/factors/{totp['id']}/verify"
        body = self._post_json(verify, {"stateToken": body["stateToken"],
                                        "passCode": pyotp.TOTP(self._totp_secret).now()})
        token = body.get("sessionToken")
        if not token:
            raise LoginFailed(f"TOTP verify returned status {body.get('status')!r}")
        return token

    def _exchange(self, opener, redirects, session_token):
        """Trade the session token for athenahealth's own cookies."""
        with opener.open(self._login_url, timeout=self._timeout) as response:
            landed = response.geturl()

        authorize = next((u for u in redirects.chain if "/v1/authorize" in u), None)
        if not authorize:
            raise LoginFailed(f"no Okta authorize step in the chain (landed {landed[:80]})")

        params = urllib.parse.parse_qs(urllib.parse.urlparse(authorize).query)
        # prompt=login makes Okta ignore the sessionToken and render its
        # sign-in page, which is the whole thing we are avoiding.
        params.pop("prompt", None)
        params["sessionToken"] = [session_token]
        url = (authorize.split("?")[0] + "?"
               + urllib.parse.urlencode({k: v[0] for k, v in params.items()}))

        with opener.open(url, timeout=self._timeout) as response:
            page = response.read().decode("utf-8", "replace")

        form = re.search(r'<form[^>]*action="([^"]+)"(.*?)</form>', page, re.S | re.I)
        if not form:
            raise LoginFailed(
                "authorize did not return an authorization code — the sessionToken "
                "was probably rejected")

        action = htmlmod.unescape(form.group(1))
        fields = {name: htmlmod.unescape(value) for name, value in
                  re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', form.group(2))}
        if "code" not in fields:
            raise LoginFailed(f"no code in the form_post, only {list(fields)}")

        request = urllib.request.Request(
            action, data=urllib.parse.urlencode(fields).encode())
        with opener.open(request, timeout=self._timeout) as response:
            return response.geturl()

    def _discover_prefix(self, landed):
        """The URL prefix get_jwt lives under.

        Login lands on /1/1/login/oidc.esp, and get_jwt works under that
        /1/1/ prefix — verified. An earlier version of this went hunting
        for the practice-scoped /32817/15/ prefix instead, on the
        assumption it was required. It is not, and looking for it failed
        because nothing after login links to it.

        The practice id is a separate thing: it travels in the
        x-athena-context header and comes from configuration, not from the
        URL.
        """
        found = APP_PREFIX.match(landed.rsplit("/", 1)[0] + "/")
        if not found:
            raise LoginFailed(f"unexpected landing URL after login: {landed[:90]}")
        return found.group(1)

    def _read_session_timeout(self):
        """athenahealth publishes its idle timeout in seconds. Use it rather
        than guessing when a re-login is due."""
        for cookie in self._jar:
            if cookie.name.lstrip(".") == "TIMEOUT_UNENCRYPTED" and cookie.value:
                try:
                    return int(cookie.value)
                except ValueError:
                    pass
        return DEFAULT_SESSION_TIMEOUT_S

    def login(self):
        """Establish a session. Safe to call again — it replaces the old one."""
        started = time.time()
        opener, redirects = self._new_opener()
        landed = self._exchange(opener, redirects, self._session_token())

        self._opener = opener
        self._prefix = self._discover_prefix(landed)
        self._session_timeout_s = self._read_session_timeout()
        self._logged_in_at = time.time()
        return {"seconds": round(time.time() - started, 2),
                "prefix": self._prefix,
                "practice": self._practice,
                "sessionTimeout": self._session_timeout_s}

    # -- using it -------------------------------------------------------

    @property
    def seconds_until_session_expiry(self):
        """Idle timeout remaining. Every request we make resets it upstream,
        so this is a floor rather than a countdown to certain death."""
        if not self._logged_in_at:
            return 0
        return max(0, self._session_timeout_s - (time.time() - self._logged_in_at))

    def mint_token(self, scopes=CARE_MANAGEMENT_SCOPES):
        """A fresh bearer token. Returns (jwt, seconds_valid)."""
        if not self._opener or not self._prefix:
            raise LoginFailed("not logged in")
        query = "&".join("scopes[]=" + urllib.parse.quote(s, safe="") for s in scopes)
        url = f"{self._prefix}{JWT_ENDPOINT}?{query}"
        try:
            with self._opener.open(url, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LoginFailed(f"get_jwt: HTTP {exc.code}") from exc
        except ValueError as exc:
            raise LoginFailed("get_jwt did not return JSON — session probably expired") from exc
        if not payload.get("jwt"):
            raise LoginFailed(f"get_jwt returned no jwt: {list(payload)}")
        # Touching the app resets the idle timer upstream, so record it.
        self._logged_in_at = time.time()
        return payload["jwt"], int(payload.get("expires_in") or 0)

    def api_headers(self, token):
        """The four headers the Care Management API requires."""
        headers = {
            "authorization": f"Bearer {token}",
            "x-athena-context": self._practice,
            "x-athena-environment": self._environment,
        }
        if self._department:
            headers["x-athena-department"] = str(self._department)
        return headers
