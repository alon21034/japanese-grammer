from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    default_host = os.getenv("WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    default_port = _env_int("WEBHOOK_PORT", 8000)
    default_reload = _env_bool("WEBHOOK_RELOAD", default=True)

    parser = argparse.ArgumentParser(
        description="Run LINE webhook FastAPI server with optional hot reload."
    )
    parser.add_argument("--host", default=default_host, help="Bind host.")
    parser.add_argument("--port", type=int, default=default_port, help="Bind port.")
    parser.add_argument(
        "--reload-dir",
        action="append",
        dest="reload_dirs",
        default=[],
        help="Extra directory to watch (can repeat).",
    )
    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload", dest="reload", action="store_true", help="Enable hot reload."
    )
    reload_group.add_argument(
        "--no-reload", dest="reload", action="store_false", help="Disable hot reload."
    )
    parser.set_defaults(reload=default_reload)
    args = parser.parse_args()

    reload_dirs = args.reload_dirs or [str(root / "src"), str(root / "scripts")]

    uvicorn.run(
        "jp_daily_line_bot.webhook_app:app",
        app_dir=str(root / "src"),
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=reload_dirs if args.reload else None,
    )


if __name__ == "__main__":
    main()
