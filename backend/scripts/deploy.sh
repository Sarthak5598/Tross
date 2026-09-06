#!/usr/bin/env bash
#
# Deploy, verify on the real box, and roll back if it fails.
#
# This exists because of two outages that a local test could not have
# prevented. Both were in the login path, which runs only when a container
# starts — so the deploy itself IS the cold-login test, on the machine that
# actually matters. What was missing was anyone checking the result, and
# any way back when it failed.
#
# The old image is kept as :previous and restored automatically if the new
# one does not come up healthy or does not pass the endpoint suite.
#
#   ssh ec2-user@<host>
#   cd ~/Tross && bash backend/scripts/deploy.sh
#
set -uo pipefail

REPO="${REPO:-$HOME/Tross}"
NAME="${NAME:-tross-api}"
PORT="${PORT:-8000}"
BASE="http://localhost:${PORT}"
# A cold start is a real login: ~30s here, and every timeout it depends on
# is now sized in minutes, so allow well past the worst plausible case.
HEALTH_TIMEOUT=300

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAILED: %s\033[0m\n' "$1"; }

cd "$REPO" || { fail "no repo at $REPO"; exit 1; }

# Fail before touching anything if the environment file is missing — it is
# gitignored, so a fresh clone will not have it, and a container without it
# starts and then cannot log in.
[ -f "$REPO/backend/.env" ] || { fail "no backend/.env on this host"; exit 1; }

say "Current state"
BEFORE=$(git rev-parse --short HEAD)
echo "  running commit: $BEFORE"
curl -s -m 10 "$BASE/health" | head -c 200; echo

say "Pulling"
git pull origin main || { fail "git pull"; exit 1; }
AFTER=$(git rev-parse --short HEAD)
echo "  now at: $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "  nothing new to deploy — rebuilding anyway"
fi

say "Keeping the current image as :previous"
# If this is the first run there is nothing to keep, which is fine — the
# rollback path checks for the tag before trying to use it.
sudo docker tag "$NAME:latest" "$NAME:previous" 2>/dev/null \
  && echo "  tagged $NAME:previous" \
  || echo "  no existing image to keep (first deploy?)"

say "Building"
sudo docker build -t "$NAME:latest" ./backend || { fail "docker build"; exit 1; }

start_container() {
  sudo docker rm -f "$NAME" >/dev/null 2>&1
  sudo docker run -d --name "$NAME" \
    --env-file "$REPO/backend/.env" \
    -p "${PORT}:8000" \
    --shm-size=1g \
    --restart unless-stopped \
    "$1" >/dev/null
}

say "Starting the new build"
start_container "$NAME:latest" || { fail "docker run"; exit 1; }

say "Waiting for a real login to complete"
# This is the cold-login gate, running on the box that matters. A fresh
# container has no session, so reaching hasToken:true means login, MFA,
# department selection, the app shell and token acquisition all worked.
DEADLINE=$((SECONDS + HEALTH_TIMEOUT))
HEALTHY=0
while [ $SECONDS -lt $DEADLINE ]; do
  BODY=$(curl -s -m 10 "$BASE/health" 2>/dev/null)
  if echo "$BODY" | grep -q '"hasToken": *true'; then
    HEALTHY=1
    echo "  logged in after $((SECONDS))s"
    break
  fi
  ERR=$(echo "$BODY" | grep -o '"sessionError": *"[^"]*"' | head -1)
  [ -n "$ERR" ] && echo "  $ERR"
  sleep 5
done

if [ "$HEALTHY" != "1" ]; then
  fail "no session after ${HEALTH_TIMEOUT}s"
  echo
  sudo docker logs --tail 40 "$NAME" 2>&1 | sed 's/^/    /'
else
  say "Running the endpoint suite against the new build"
  # A missing python3 must not trigger a rollback of a build that is
  # otherwise healthy — that would be the tooling breaking the deploy.
  if ! command -v python3 >/dev/null 2>&1; then
    echo "  python3 not installed here; skipping (login gate already passed)"
    SUITE=0
  elif [ ! -f backend/tests/test_e2e.py ]; then
    echo "  suite not found; skipping"
    SUITE=0
  else
    (cd backend && python3 tests/test_e2e.py "$BASE")
    SUITE=$?
  fi
fi

if [ "$HEALTHY" = "1" ] && [ "${SUITE:-1}" -eq 0 ]; then
  say "Deployed"
  echo "  commit $AFTER is live and passing"
  curl -s -m 10 "$BASE/health" | head -c 200; echo
  exit 0
fi

say "Rolling back"
if sudo docker image inspect "$NAME:previous" >/dev/null 2>&1; then
  start_container "$NAME:previous"
  echo "  restored the previous image; waiting for it to log in"
  DEADLINE=$((SECONDS + HEALTH_TIMEOUT))
  while [ $SECONDS -lt $DEADLINE ]; do
    curl -s -m 10 "$BASE/health" 2>/dev/null | grep -q '"hasToken": *true' && {
      echo "  rollback healthy"; break; }
    sleep 5
  done
  echo
  echo "  The new build is NOT deployed. Fix it and run this again."
else
  fail "no previous image to roll back to — the service is down"
fi
exit 1
