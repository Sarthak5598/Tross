import os

from dotenv import load_dotenv

load_dotenv()

ATHENA_LOGIN_URL = os.environ["ATHENA_LOGIN_URL"]
ATHENA_USERNAME = os.environ["ATHENA_USERNAME"]
ATHENA_PASSWORD = os.environ["ATHENA_PASSWORD"]
ATHENA_TOTP_SECRET = os.environ["ATHENA_TOTP_SECRET"]

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
