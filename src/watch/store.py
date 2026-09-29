"""state/ の JSON 読み書き。原子的書き込み＋決定的シリアライズ（ensure_ascii=False, indent=1, sort_keys=True）。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


def write_json(path: Path, value: Any) -> bool:
    """一時ファイル→置換。内容が同一なら何もしない。書き込んだら True。"""
    path = Path(path)
    payload = dumps(value).encode("utf-8")
    if path.exists() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    # -- パス
    @property
    def membership_path(self) -> Path:
        return self.root / "jpx400_membership.json"

    @property
    def adjustments_path(self) -> Path:
        return self.root / "jpx400_adjustments.json"

    def ranking_path(self, kind: str, key: str) -> Path:
        return self.root / "rankings" / kind / f"{key}.json"

    # -- events / episodes / setup_history
    def write_events(self, by_month: dict[str, list[dict]]) -> None:
        for month, events in by_month.items():
            write_json(self.root / "events" / f"{month}.json",
                       {"schema_version": 1, "month": month, "events": events})

    def write_episodes(self, episodes: list[dict]) -> None:
        links = [{"episode_id": e["episode_id"], "event_id": eid} for e in episodes for eid in e["event_ids"]]
        links.sort(key=lambda x: (x["episode_id"], x["event_id"]))
        write_json(self.root / "episodes.json", {"schema_version": 1, "episodes": episodes, "episode_events": links})

    def write_setup_history(self, changes: list[dict]) -> None:
        by_month: dict[str, list[dict]] = {}
        for c in changes:
            by_month.setdefault(c["date"][:7], []).append(c)
        for month, items in by_month.items():
            items.sort(key=lambda c: (c["date"], c["security_code"], c["kind"], c.get("milestone") or ""))
            write_json(self.root / "setup_history" / f"{month}.json",
                       {"schema_version": 1, "month": month, "changes": items})

    # -- rankings
    def read_ranking(self, kind: str, key: str) -> dict | None:
        return read_json(self.ranking_path(kind, key))

    def write_ranking(self, kind: str, key: str, snap: dict) -> bool:
        """final 済みのスナップショットは書き換えない。"""
        existing = self.read_ranking(kind, key)
        if existing and existing.get("final"):
            return False
        return write_json(self.ranking_path(kind, key), snap)

    # -- margin
    def read_margin_history(self) -> dict:
        folder = self.root / "margin" / "history"
        files = sorted(folder.glob("*.json")) if folder.exists() else []
        return read_json(files[-1], {}) if files else {}

    def write_margin(self, latest: dict, history: dict) -> None:
        write_json(self.root / "margin" / "latest.json", latest)
        write_json(self.root / "margin" / "history" / f"{latest['as_of'][:7]}.json", history)

    def read_margin_latest(self) -> dict | None:
        return read_json(self.root / "margin" / "latest.json")

    # -- job_runs
    def append_job_runs(self, month: str, runs: list[dict], keep: int = 400) -> None:
        path = self.root / "job_runs" / f"{month}.json"
        existing = read_json(path, [])
        write_json(path, (existing + runs)[-keep:])

    def last_success(self, source: str) -> str | None:
        folder = self.root / "job_runs"
        for path in sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []:
            for run in reversed(read_json(path, [])):
                if run.get("source") == source and run.get("status") in ("ok", "partial"):
                    return run.get("finished_at")
        return None
