from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self.default = default

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(self.default)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return dict(self.default)
        if not isinstance(data, dict):
            return dict(self.default)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def subscribers_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "subscribers.json", default={"user_ids": []})


def progress_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "progress.json", default={"sent_urls": []})


def nhk_progress_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "nhk_progress.json", default={"sent_news_ids": []})


def quiz_state_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "quiz_state.json", default={"users": {}})


def local_ui_state_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "local_ui_state.json", default={"users": {}})


def grammar_explain_cache_store(data_dir: Path) -> JsonStore:
    return JsonStore(path=data_dir / "grammar_explain_cache.json", default={"items": {}})
