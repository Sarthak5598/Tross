# Deploying the API

## The one constraint that drives everything

**This must run as exactly one always-on process.** The authenticated
athena session, the job lock, the idempotency cache and job/frame storage
all live in process memory. Two instances would each hold their own login
and silently disagree; a process that gets recycled between requests would
re-login every time (~30s, the cost we spent this whole project removing).

That rules out **Lambda**, **App Runner**, and any autoscaling group with
more than one instance. A single EC2 instance running Docker is the right
shape, and `--workers 1` in the Dockerfile is load-bearing — don't raise it.

## 1. Launch the instance

- **Region:** any US region (`us-east-1` is cheapest). Hosting in the US
  also means the `PROXY_*` vars in `.env` can be left empty — they existed
  to reach the sandbox from outside the US.
- **Type:** `t3.medium` (2 vCPU / 4GB). `t3.small` works for the current
  one-request-at-a-time setup, but Chromium rendering the Care Management
  pane is memory-hungry and the planned concurrency work wants the headroom.
  Roughly $30/month on-demand.
- **Storage:** 30GB gp3 is plenty (the image is ~1.5GB with Chromium).
- **OS:** Amazon Linux 2023 or Ubuntu 22.04+.

## 2. Lock down access — read this before opening a port

The API serves **medical records with no authentication**, and
`/api/jobs/{id}/frames/{n}` returns screenshots of patient charts. Do not
attach a security group that allows `0.0.0.0/0` on the API port.

Pick one:

**(a) SSH tunnel — recommended.** No inbound API port at all, no TLS to
configure, nothing exposed to the internet. Security group allows only SSH
(22) from your IP. From your machine:

```bash
ssh -i key.pem -L 8000:localhost:8000 ec2-user@<instance-ip>
```

Then the dashboard talks to `http://127.0.0.1:8000` as if it were local.

**(b) Security-group allowlist.** Open 8000 only to your specific office/VPN
IP. Simpler for a shared team, but traffic is still plain HTTP — fine on a
private network, not over the open internet.

**(c) ALB + ACM certificate.** Proper HTTPS. Worth it once this is more than
a demo, and the point at which real API auth should exist too. Note an ALB's
default idle timeout is 60s — raise it, since `wait=true` requests can run
for minutes.

## 3. Install Docker

```bash
sudo dnf install -y docker && sudo systemctl enable --now docker && sudo usermod -aG docker $USER
```

(Ubuntu: `sudo apt-get update && sudo apt-get install -y docker.io`, same
`systemctl`/`usermod` lines.) Log out and back in for the group to apply.

## 4. Get the code and create the .env

Copy the project up (git clone, or `scp -r`). Then create `.env` **on the
server** — it is deliberately excluded from the image by `.dockerignore`, so
credentials are never baked into a layer:

```bash
cat > .env <<'EOF'
ATHENA_LOGIN_URL=https://preview.athenahealth.com/...
ATHENA_USERNAME=...
ATHENA_PASSWORD=...
ATHENA_TOTP_SECRET=...
EOF
chmod 600 .env
```

Longer term these belong in AWS Secrets Manager rather than a file — note
that the TOTP secret sitting next to the password means the second factor
isn't really a second factor, which is unavoidable for automation but worth
being deliberate about.

## 5. Build and run

```bash
docker build -t tross-api .

docker run -d --name tross-api \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  --shm-size=1g \
  --restart unless-stopped \
  tross-api
```

- `-p 127.0.0.1:8000:8000` binds to loopback only, so the port isn't
  reachable from outside even if the security group is wrong. Use plain
  `-p 8000:8000` only with option (b) or (c) above.
- `--shm-size=1g` matters: Docker's default `/dev/shm` is 64MB and Chromium
  crashes on heavy pages without it. (The image also passes
  `--disable-dev-shm-usage` as a belt-and-braces measure.)
- `--restart unless-stopped` brings it back after a reboot or a crash.

## 6. Verify

```bash
curl localhost:8000/health
# {"status":"ok","activeJobId":null}

# End-to-end, with a real status code:
curl -i -X POST "localhost:8000/api/patient?patient_id=1133&sections=summary&wait=true&department=SH%20OH%20-%20Shaker"
```

Expect 200 with the plan summary. The first call includes a ~30s login; the
next reuses the session.

Sanity-check the error paths too — they should be 404 and not a hang:

```bash
curl -i -X POST "localhost:8000/api/patient?patient_id=9999999&sections=&wait=true"
```

## 7. Point the dashboard at it

```bash
TROSS_API_BASE=http://127.0.0.1:8000 streamlit run streamlit_app.py
```

(With the SSH tunnel from step 2a, `127.0.0.1:8000` is the deployed server.)

## Operating notes

- **Logs:** `docker logs -f tross-api`. There's no structured logging yet;
  per-job step logs live in the API response, not stdout.
- **Restarts lose state.** Jobs, results and the warm session are all
  in-memory by design. A restart just means the next request pays for a
  fresh login.
- **`/health` reports `activeJobId`.** If that's non-null for longer than
  `JOB_TIMEOUT_S` (300s), something is genuinely wedged — though the job
  timeout should now fail it and release the lock on its own.
- **Concurrency is currently 1.** A second simultaneous request gets `409`
  with the running job's id. The validated design for raising this (one
  BrowserContext per slot) is written up in TROUBLESHOOTING.md #26.
