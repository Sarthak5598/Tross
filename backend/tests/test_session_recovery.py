"""Does a wedged session actually rebuild itself?

The real failure looked like: is_logged_in() says the page is healthy
(search box visible), but search_patient dies on the dropdown. The
renewal loop then retried the same wedged page every 30s forever.

Simulated by making the first _attempt raise the exact error observed.
"""
import asyncio
from automation import browser_pool
from automation.api_session import session

async def main():
    await session.warm_up()
    print("  baseline: token acquired OK")

    real_attempt = session._attempt
    reset_called = {"n": 0}
    real_reset = browser_pool.reset_page
    async def counting_reset():
        reset_called["n"] += 1
        await real_reset()
    browser_pool.reset_page = counting_reset

    calls = {"n": 0}
    async def flaky(on_step):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Could not locate 'Patient ID' option in search dropdown")
        return await real_attempt(on_step)
    session._attempt = flaky

    await session.invalidate()
    try:
        client = await session.client()
        print(f"  after wedge: RECOVERED  attempts={calls['n']} "
              f"reset_page_called={reset_called['n']} "
              f"token={'yes' if client.token else 'no'}")
    except Exception as exc:
        print(f"  after wedge: STILL BROKEN -> {exc}")
    finally:
        session._attempt = real_attempt
        browser_pool.reset_page = real_reset

    print("  /health lastError:", session.status().get("lastError"))

asyncio.run(main())
