#!/usr/bin/env bash
# Ship this checkout to every deployed host and prove the code actually landed.
#
#   scripts/deploy.sh                 # all hosts in HOSTS
#   scripts/deploy.sh anr-vps         # one host
#
# Why this exists: a hand-rolled `tar | scp | ssh tar x` once extracted a stale
# tarball on the second host, so that box ran old code while looking healthy.
# The stamp + schema check at the end makes that impossible to miss.
set -euo pipefail

if [ "$#" -gt 0 ]; then
  HOSTS=("$@")
else
  HOSTS=(anr-vps anr-jump)
fi
REMOTE_DIR=${REMOTE_DIR:-/opt/ai-news-radar}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
PAYLOAD=$(mktemp -t anr-deploy-XXXXXX.tgz)
trap 'rm -f "$PAYLOAD"' EXIT

cd "$(dirname "$0")/.."
tar czf "$PAYLOAD" app config scripts tests requirements.txt docs

failures=()
for host in "${HOSTS[@]}"; do
  echo "==> $host"
  scp -q -o BatchMode=yes "$PAYLOAD" "$host:/tmp/anr_deploy.tgz"
  if ssh -o BatchMode=yes "$host" "STAMP='$STAMP' REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"
tar xzf /tmp/anr_deploy.tgz
chown -R news:news "$REMOTE_DIR" 2>/dev/null || true
echo "$STAMP" > .deploy_stamp
# Deploying twice in a minute trips the unit's StartLimitIntervalSec; the app is
# fine, systemd just refuses to start it that fast. Reset once and retry.
systemctl restart ai-news-radar || true
sleep 6
# `systemctl is-active` exits non-zero for a failed unit, so under `set -e`
# reading it naively aborts the script and the self-heal below never runs.
state=$(systemctl is-active ai-news-radar || true)
if [ "$state" != active ]; then
  systemctl reset-failed ai-news-radar || true
  systemctl start ai-news-radar || true
  sleep 6
  state=$(systemctl is-active ai-news-radar || true)
fi
schema=$(sudo -u news .venv/bin/python - <<'PY'
from sqlalchemy import inspect
from app.config import get_config
from app.database.database import get_engine, init_db
init_db()
cols = {r["name"] for r in inspect(get_engine(get_config().settings.sqlalchemy_url)).get_columns("articles")}
print("ok" if "free_offer_sent_at" in cols else "MISSING-COLUMN")
PY
)
printf '    service=%s schema=%s stamp=%s\n' "$state" "$schema" "$(cat .deploy_stamp)"
if [ "$state" != active ]; then
  echo "    FAILED: service not active; last log lines:"
  journalctl -u ai-news-radar -n 8 --no-pager --output=short-iso | sed 's/^/    | /'
  exit 1
fi
[ "$schema" = ok ] || { echo "    FAILED: migration did not apply"; exit 1; }
REMOTE
  then
    echo "    ok"
  else
    failures+=("$host")
  fi
done

if [ "${#failures[@]}" -gt 0 ]; then
  echo "==> FAILED hosts: ${failures[*]}"
  exit 1
fi
echo "==> deployed to: ${HOSTS[*]}"
