# Linux PC での TOPIX Watch Radar 日次実行

```bash
# 1. リポジトリと venv
git clone https://github.com/highwing-republic/stock ~/stock
cd ~/stock && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # EDINET_API_KEY を入れる（無ければ EDINET 更新はスキップされる）

# 2. 初回: EDINET 90 日 → TOPIX 初回実行（986 銘柄の価格取得に 5〜10 分）
.venv/bin/python scripts/update.py --days 90
.venv/bin/python scripts/update_watch.py --universe topix

# 3. systemd user timer
mkdir -p ~/.config/systemd/user
cp deploy/systemd/topix-watch.service deploy/systemd/topix-watch.timer deploy/systemd/topix-watch-notify.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now topix-watch.timer
loginctl enable-linger "$USER"

# 動作確認
systemctl --user list-timers topix-watch.timer
systemctl --user start topix-watch.service && journalctl --user -u topix-watch.service -n 50
```

push には書き込み可能な deploy key か fine-grained PAT が必要。`git config credential.helper` か SSH 鍵で設定し、
`~/.git-credentials` に平文で置かない。

既に JPX400 用の cron がある場合は、その cron を止めて `run_topix_daily.sh` に統合する
（`update_watch.py` を `--universe jpx400` でもう 1 行呼ぶ）。
