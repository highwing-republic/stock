#!/usr/bin/env bash
# TOPIX Watch Radar の日次実行（Linux PC 用）。systemd timer から呼ぶ。
#   1. git pull --rebase
#   2. EDINET v1 更新（直近 7 日。EDINET_API_KEY が無ければスキップ）
#   3. update_watch.py --universe topix
#   4. tests/watch のテスト
#   5. state/topix と public/data/watch-topix に差分があれば commit & push
# 多重起動は flock で防ぐ。失敗したら非 0 で終了（systemd の OnFailure で通知）。
set -euo pipefail

REPO="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
LOCK="${LOCK_FILE:-/tmp/topix-watch.lock}"
BRANCH="${GIT_BRANCH:-main}"
EDINET_DAYS="${EDINET_DAYS:-7}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another run holds $LOCK; exiting" >&2
  exit 0
fi

cd "$REPO"
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

echo "== $(date -Is) git pull"
git pull --rebase --quiet origin "$BRANCH"

if [ -n "${EDINET_API_KEY:-}" ]; then
  echo "== $(date -Is) update.py --days $EDINET_DAYS"
  "$PY" scripts/update.py --days "$EDINET_DAYS" || echo "update.py failed; continuing with cached filings" >&2
else
  echo "== EDINET_API_KEY not set; skipping update.py (events will come from the cached DB only)" >&2
fi

echo "== $(date -Is) update_watch.py --universe topix"
"$PY" scripts/update_watch.py --universe topix

echo "== $(date -Is) pytest"
"$PY" -m pytest -q tests/watch

echo "== $(date -Is) commit"
git add state/topix public/data/watch-topix
if git diff --cached --quiet; then
  echo "no changes"
  exit 0
fi
git -c user.name="${GIT_USER_NAME:-topix-watch}" -c user.email="${GIT_USER_EMAIL:-topix-watch@localhost}" \
  commit --quiet -m "data: update TOPIX watch radar"
if ! git push --quiet origin "HEAD:$BRANCH"; then
  echo "push rejected; rebasing once" >&2
  git pull --rebase --quiet origin "$BRANCH"
  git push --quiet origin "HEAD:$BRANCH"
fi
echo "== $(date -Is) done"
