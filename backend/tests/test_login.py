"""Forces a genuine cold login. Run this before deploying ANY change that
touches login, browser_pool, api_session or token_source.

Both production outages in this project came from the login path, which
only executes when there is no session — so a warm process, a passing e2e
suite and a green unit run all say nothing about it. The only way to know
is to throw the session away and log in for real.

Takes ~40s and consumes a TOTP window, so it is not part of the fast
suite. It is the gate before deploying browser-side changes.

    python tests/test_login.py
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from automation import browser_pool
from automation.api_session import session

FAILED = []

async def http_login_gate():
    """The browserless path, which is now primary.

    Cheap enough (~15s, no browser) that there is no excuse for skipping
    it, and it exercises the exact thing that broke twice: RE-login on an
    object that already has a session.
    """
    import config
    from automation.http_login import AthenaHttpSession

    print("  -- browserless login (primary path)")
    session = AthenaHttpSession(
        login_url=config.ATHENA_LOGIN_URL, username=config.ATHENA_USERNAME,
        password=config.ATHENA_PASSWORD, totp_secret=config.ATHENA_TOTP_SECRET,
        environment=config.ATHENA_ENVIRONMENT, practice=config.ATHENA_PRACTICE,
        department=config.ATHENA_DEPARTMENT)

    info = await asyncio.get_running_loop().run_in_executor(None, session.login)
    print(f"     login {info['seconds']}s  idle budget {info['sessionTimeout']}s")

    token, ttl = await asyncio.get_running_loop().run_in_executor(None, session.mint_token)
    if not token or ttl <= 0:
        FAILED.append("http login minted no usable token")
    print(f"     token ttl={ttl}s")

    # the failure mode that caused both outages
    again = await asyncio.get_running_loop().run_in_executor(None, session.login)
    print(f"     re-login {again['seconds']}s")
    token2, ttl2 = await asyncio.get_running_loop().run_in_executor(None, session.mint_token)
    if not token2:
        FAILED.append("http re-login produced no token")
    if token2 == token:
        FAILED.append("re-login returned the same token")
    print(f"     token after re-login ttl={ttl2}s, different={token2 != token}")

    headers = session.api_headers(token2)
    missing = {"authorization", "x-athena-context", "x-athena-environment"} - set(headers)
    if missing:
        FAILED.append(f"api_headers missing {sorted(missing)}")
    print(f"     headers: {sorted(headers)}")


async def main():
    await http_login_gate()
    print(chr(10) + "  -- browser login (fallback path)")
    steps = []
    async def on_step(message):
        steps.append(f"{time.time() - t0:6.1f}s  {message}")

    await browser_pool.start()
    await browser_pool.reset_page()      # guarantee nothing is reusable
    t0 = time.time()

    try:
        await session._ensure_logged_in(on_step)
        print(f"  cold login OK in {time.time() - t0:.1f}s")
    except Exception as exc:
        FAILED.append(f"login failed after {time.time()-t0:.1f}s: {str(exc)[:200]}")
        for line in steps:
            print("   ", line)
        await browser_pool.stop()
        return

    for line in steps:
        print("   ", line)

    await session._provoke_graphql(on_step)
    headers = session._capture.headers or {}
    wanted = {"authorization", "x-athena-context", "x-athena-environment", "x-athena-department"}
    missing = wanted - set(headers)
    if missing:
        FAILED.append(f"headers missing after login: {sorted(missing)}")
    print(f"\n  headers captured: {sorted(headers)}")

    token, _ = await session._tokens.get()
    if not token:
        FAILED.append("no token after a successful login")
    print(f"  token remaining: {session.status()['secondsRemaining']}s")

    # minting is the normal renewal path — prove it works on a fresh session
    started = time.time()
    minted = await session._mint(on_step)
    if not minted:
        FAILED.append("token endpoint did not mint on a fresh session")
    else:
        print(f"  minted a replacement in {time.time()-started:.2f}s")

    client = await session.client()
    plan = await client.run("GetPatientCarePlanInternal", patientId="1133")
    concerns = (plan.get("getPatientCarePlanInternal") or {}).get("HealthConcerns") or []
    if not concerns:
        FAILED.append("GraphQL returned no concerns after a cold login")
    print(f"  GraphQL after cold login: {len(concerns)} concerns")

    await browser_pool.stop()

asyncio.run(main())
print()
if FAILED:
    for f in FAILED:
        print(f"  FAIL  {f}")
    sys.exit(1)
print("  cold login gate: PASSED")
