#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jp_daily_line_bot.codex_offline import build_detailed_explanations_offline
from jp_daily_line_bot.grammar_rag import retrieve_grammar_references


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich local NHK Easy article JSON with grammar references and offline detailed explanations."
    )
    parser.add_argument(
        "--data-dir",
        default="data/nhk_easy",
        help="NHK data directory (default: data/nhk_easy)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Maximum number of local article files to process (0 means all).",
    )
    parser.set_defaults(missing_only=True)
    parser.add_argument(
        "--missing-only",
        dest="missing_only",
        action="store_true",
        help="Only process files missing grammar_references or offline_detailed_explanations (default).",
    )
    parser.add_argument(
        "--all",
        dest="missing_only",
        action="store_false",
        help="Rebuild enrichment for all local article files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file progress logs.",
    )
    return parser


def _load_article(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_lines(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(line).strip() for line in raw if str(line).strip()]


def _has_non_empty_list_field(payload: dict[str, Any], field: str) -> bool:
    raw = payload.get(field)
    if not isinstance(raw, list):
        return False
    return any(isinstance(item, dict) for item in raw)


def enrich(
    data_dir: Path,
    *,
    max_files: int,
    missing_only: bool,
    verbose: bool,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    articles_dir = data_dir / "articles"
    if not articles_dir.exists():
        raise RuntimeError(f"Articles directory not found: {articles_dir}")

    files = sorted(articles_dir.glob("*.json"))
    if max_files > 0:
        files = files[:max_files]

    project_data_dir = data_dir.parent
    summary: dict[str, Any] = {
        "started_at": started_at,
        "mode": "missing-only" if missing_only else "all",
        "total_files": len(files),
        "processed": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "failed_files": [],
    }

    for i, path in enumerate(files, start=1):
        payload = _load_article(path)
        if not payload:
            summary["failed"] += 1
            summary["failed_files"].append({"file": str(path), "error": "Invalid JSON payload"})
            continue

        has_refs = _has_non_empty_list_field(payload, "grammar_references")
        has_details = _has_non_empty_list_field(payload, "offline_detailed_explanations")
        if missing_only and has_refs and has_details:
            summary["skipped"] += 1
            continue

        title = str(payload.get("title", "")).strip()
        paragraphs_plain = _clean_lines(payload.get("paragraphs_plain"))
        if not paragraphs_plain:
            summary["failed"] += 1
            summary["failed_files"].append({"file": str(path), "error": "Missing paragraphs_plain"})
            continue

        try:
            refs = retrieve_grammar_references(
                project_data_dir,
                article_title=title,
                article_paragraphs=paragraphs_plain,
                top_k=3,
            )
            details = build_detailed_explanations_offline(
                article_title=title,
                article_paragraphs=paragraphs_plain,
                grammar_references=refs,
            )
        except Exception as exc:  # pragma: no cover
            summary["failed"] += 1
            summary["failed_files"].append({"file": str(path), "error": str(exc)})
            continue

        summary["processed"] += 1

        prev_refs = payload.get("grammar_references")
        prev_details = payload.get("offline_detailed_explanations")
        changed = prev_refs != refs or prev_details != details
        if not changed:
            summary["unchanged"] += 1
            if verbose:
                print(f"[{i}/{len(files)}] unchanged {path.name}", flush=True)
            continue

        payload["grammar_references"] = refs
        payload["offline_detailed_explanations"] = details
        payload["enriched_at"] = utc_now_iso()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        summary["updated"] += 1
        if verbose:
            print(f"[{i}/{len(files)}] updated {path.name}", flush=True)

    summary["finished_at"] = utc_now_iso()
    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()

    summary = enrich(
        data_dir=data_dir,
        max_files=max(args.max, 0),
        missing_only=bool(args.missing_only),
        verbose=bool(args.verbose),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
