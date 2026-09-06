"""Strip secrets out of anything that leaves the process.

This exists because of a real leak, not as a precaution. A failed login
produced a Playwright error whose text embedded the DOM element it was
waiting on:

    locator resolved to <input disabled ... value="<the real password>" ...>

That text became job["error"], and _job_to_http puts job["error"] in the
response body of a public, unauthenticated endpoint. Anyone able to
trigger a login failure could read the athenahealth password.

Two independent defences, because either alone is brittle:

  * exact-value redaction, which catches the credentials we know about
    wherever they appear; and
  * stripping `value="..."` and `placeholder="..."` out of any HTML in the
    text, which catches fields we did not think of — the next secret in a
    DOM dump will not be one we remembered to add here.
"""

import re

import config

# Attribute dumps are where a browser automation error hides user input.
_VALUE_ATTR = re.compile(r'\b(value|placeholder|aria-label)="[^"]*"', re.I)

# Anything JWT-shaped. Not secret forever, but no reason to hand it out.
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")


def _secrets() -> list[str]:
    """Credential values worth removing, longest first so that redacting a
    short one cannot leave a fragment of a longer one behind."""
    values = [
        getattr(config, name, None)
        for name in ("ATHENA_PASSWORD", "ATHENA_TOTP_SECRET",
                     "ATHENA_USERNAME", "PROXY_PASSWORD", "PROXY_USERNAME")
    ]
    return sorted((v for v in values if v and len(v) >= 4), key=len, reverse=True)


def redact(text: str, limit: int = 600) -> str:
    """Make an internal error safe to return to a caller.

    Also truncated: a 3,000-character Playwright call log is not useful to
    an API client and every extra line is another chance to leak something.
    """
    if not text:
        return text
    for secret in _secrets():
        text = text.replace(secret, "[redacted]")
    text = _VALUE_ATTR.sub(r'\1="[redacted]"', text)
    text = _JWT.sub("[redacted-jwt]", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + " … (truncated)"
    return text
