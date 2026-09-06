# Tests

Not a unit-test suite — these drive a real athenahealth sandbox session
and take minutes. Run them against a server you started yourself.

```bash
uvicorn main:app --port 8041
python tests/test_e2e.py              # happy paths, error contract, concurrency
python tests/test_session_recovery.py # session self-heal (starts its own browser)
```

Every wait in these is **bounded**. An earlier ad-hoc harness polled a job
id that was never created and spun for over an hour; a test that cannot
fail is worse than no test.
