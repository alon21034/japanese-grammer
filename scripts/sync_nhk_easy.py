#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jp_daily_line_bot.nhk_easy import EASY_INDEX_URL, bootstrap_anonymous_session, fetch_article, fetch_top_news_list
from jp_daily_line_bot.grammar_rag import retrieve_grammar_references
from jp_daily_line_bot.codex_offline import build_detailed_explanations_offline


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    return data if isinstance(data, dict) else default


def dump_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def canonical_payload(title: str, news_id: str, published_at: str | None, paragraphs: list[str]) -> str:
    lines = [title, news_id, published_at or ""]
    lines.extend(paragraphs)
    return "\n".join(lines)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync NHK Easy news articles incrementally.")
    parser.add_argument(
        "--data-dir",
        default="data/nhk_easy",
        help="Output data directory (default: data/nhk_easy)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Maximum number of news items from top-list (0 means all).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay seconds between article requests (default: 0.2).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite article files even if unchanged.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-news progress logs.",
    )
    parser.set_defaults(new_only=True)
    parser.add_argument(
        "--new-only",
        dest="new_only",
        action="store_true",
        help="Only crawl news IDs not seen before in manifest (default).",
    )
    parser.add_argument(
        "--full-check",
        dest="new_only",
        action="store_false",
        help="Crawl all news IDs in source and compare hash (re-check all).",
    )
    return parser


def sync(
    data_dir: Path,
    max_items: int,
    delay_sec: float,
    force: bool,
    verbose: bool,
    new_only: bool,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    data_dir.mkdir(parents=True, exist_ok=True)
    articles_dir = data_dir / "articles"
    manifest_path = data_dir / "manifest.json"
    index_path = data_dir / "index.json"
    last_run_path = data_dir / "last_run.json"

    manifest = load_json(manifest_path, default={"articles": {}})
    existing_articles: dict[str, dict[str, Any]] = manifest.get("articles", {})
    for rec in existing_articles.values():
        if isinstance(rec, dict):
            rec["in_source"] = False

    session, token = bootstrap_anonymous_session()
    top_list = fetch_top_news_list(session, token)
    if max_items > 0:
        top_list = top_list[:max_items]

    top_ids: list[str] = []
    for item in top_list:
        news_id = str(item.get("news_id", "")).strip()
        if news_id:
            top_ids.append(news_id)

    seen = set(top_ids)
    crawl_items = (
        [item for item in top_list if str(item.get("news_id", "")).strip() not in existing_articles]
        if new_only
        else top_list
    )

    summary: dict[str, Any] = {
        "started_at": started_at,
        "source_index_url": EASY_INDEX_URL,
        "mode": "new-only" if new_only else "full-check",
        "total_items_in_source": len(top_list),
        "total_items_crawled": len(crawl_items),
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "removed_from_source": 0,
        "failed_ids": [],
    }

    for i, item in enumerate(crawl_items, start=1):
        news_id = str(item.get("news_id", "")).strip()
        if not news_id:
            continue
        if verbose:
            print(f"[{i}/{len(crawl_items)}] {news_id}", flush=True)

        now = utc_now_iso()
        try:
            article = fetch_article(session, token, news_id)
            published_at = str(item.get("news_prearranged_time", "")).strip() or None
            project_data_dir = data_dir.parent
            grammar_references = retrieve_grammar_references(
                project_data_dir,
                article_title=article.title,
                article_paragraphs=article.paragraphs_plain,
                top_k=3,
            )
            offline_detailed_explanations = build_detailed_explanations_offline(
                article_title=article.title,
                article_paragraphs=article.paragraphs_plain,
                grammar_references=grammar_references,
            )

            payload = canonical_payload(
                title=article.title,
                news_id=news_id,
                published_at=published_at,
                paragraphs=article.paragraphs_plain,
            )
            digest = sha256_hex(payload)

            prev = existing_articles.get(news_id) or {}
            prev_hash = prev.get("content_hash")
            status = "unchanged"
            if prev_hash is None:
                status = "new"
            elif prev_hash != digest:
                status = "updated"

            rel_file = f"articles/{news_id}.json"
            abs_file = data_dir / rel_file
            if force or status in {"new", "updated"} or not abs_file.exists():
                article_json = {
                    "news_id": news_id,
                    "title": article.title,
                    "url": article.url,
                    "published_at": published_at,
                    "regular_news_url": article.regular_news_url,
                    "paragraphs_plain": article.paragraphs_plain,
                    "paragraphs_with_furigana": article.paragraphs_with_furigana,
                    "grammar_references": grammar_references,
                    "offline_detailed_explanations": offline_detailed_explanations,
                    "body_html": article.body_html,
                    "top_list_item": item,
                    "scraped_at": now,
                    "content_hash": digest,
                }
                dump_json(abs_file, article_json)

            if status == "new":
                summary["new"] += 1
                first_seen_at = now
                last_changed_at = now
            elif status == "updated":
                summary["updated"] += 1
                first_seen_at = prev.get("first_seen_at") or now
                last_changed_at = now
            else:
                summary["unchanged"] += 1
                first_seen_at = prev.get("first_seen_at") or now
                last_changed_at = prev.get("last_changed_at") or now

            rec = {
                "news_id": news_id,
                "title": article.title,
                "published_at": published_at,
                "url": article.url,
                "content_hash": digest,
                "file": rel_file,
                "first_seen_at": first_seen_at,
                "last_seen_at": now,
                "last_changed_at": last_changed_at,
                "in_source": True,
            }
            existing_articles[news_id] = rec
        except Exception as exc:  # pragma: no cover
            summary["failed"] += 1
            summary["failed_ids"].append({"news_id": news_id, "error": str(exc)})
            if verbose:
                print(f"  -> failed: {exc}", flush=True)
        finally:
            if delay_sec > 0 and i < len(crawl_items):
                time.sleep(delay_sec)

    removed = 0
    for news_id, rec in existing_articles.items():
        if not isinstance(rec, dict):
            continue
        if news_id in seen:
            rec["in_source"] = True
            rec["last_seen_at"] = utc_now_iso()
        if not rec.get("in_source", False):
            removed += 1
    summary["removed_from_source"] = removed
    summary["finished_at"] = utc_now_iso()

    index_records = [existing_articles[nid] for nid in top_ids if nid in existing_articles]
    output_manifest = {
        "source_index_url": EASY_INDEX_URL,
        "last_sync_at": summary["finished_at"],
        "total_articles_known": len(existing_articles),
        "articles": existing_articles,
    }
    dump_json(manifest_path, output_manifest)
    dump_json(index_path, {"generated_at": summary["finished_at"], "items": index_records})
    dump_json(last_run_path, summary)
    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()

    summary = sync(
        data_dir=data_dir,
        max_items=max(args.max, 0),
        delay_sec=max(args.delay, 0.0),
        force=bool(args.force),
        verbose=bool(args.verbose),
        new_only=bool(args.new_only),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
