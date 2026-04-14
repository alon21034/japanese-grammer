from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    line_channel_access_token: str
    line_channel_secret: str
    line_user_id: str | None
    openai_api_key: str | None
    openai_model: str
    local_test_mode: bool
    local_test_user_id: str
    data_dir: Path


def load_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    _load_dotenv(root / ".env")

    access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    line_user_id = os.getenv("LINE_USER_ID", "").strip() or None
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    local_test_mode = _env_bool("LOCAL_TEST_MODE", default=False)
    local_test_user_id = os.getenv("LOCAL_TEST_USER_ID", "").strip() or "Ulocaltest"

    data_dir_raw = os.getenv("DATA_DIR", "./data").strip() or "./data"
    data_dir = (root / data_dir_raw).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        line_channel_access_token=access_token,
        line_channel_secret=channel_secret,
        line_user_id=line_user_id,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        local_test_mode=local_test_mode,
        local_test_user_id=local_test_user_id,
        data_dir=data_dir,
    )
