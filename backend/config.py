import os

from dotenv import load_dotenv

load_dotenv()

ATHENA_LOGIN_URL = os.environ["ATHENA_LOGIN_URL"]
ATHENA_USERNAME = os.environ["ATHENA_USERNAME"]
ATHENA_PASSWORD = os.environ["ATHENA_PASSWORD"]
ATHENA_TOTP_SECRET = os.environ["ATHENA_TOTP_SECRET"]

# Values the browser used to supply as request headers. They are static
# per environment, so with the browserless login they come from here
# instead of being scraped off a captured request.
ATHENA_ENVIRONMENT = os.environ.get("ATHENA_ENVIRONMENT", "preview@nva")
ATHENA_PRACTICE = os.environ.get("ATHENA_PRACTICE", "32817")
ATHENA_DEPARTMENT = os.environ.get("ATHENA_DEPARTMENT", "4")

# Set to "0" to force the old browser login. The HTTP path is the default:
# it is faster, deterministic, and has none of the failure modes that come
# from driving a web form.
USE_HTTP_LOGIN = os.environ.get("USE_HTTP_LOGIN", "1") != "0"

PROXY_SERVER = os.environ.get("PROXY_SERVER") or None
PROXY_USERNAME = os.environ.get("PROXY_USERNAME") or None
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD") or None


def proxy_config() -> dict | None:
    if not PROXY_SERVER:
        return None
    proxy = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy
