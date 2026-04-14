#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jp_daily_line_bot.mainichi import GRAMMAR_INDEX_URL, fetch_article, fetch_grammar_urls


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


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "root"
    slug = path.replace("/", "__")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug)
    return slug.strip("-") or "article"


def normalize_level(title: str) -> str | None:
    # e.g. "〖Ｎ１文法〗～放題"
    match = re.search(r"[ＮN]([０-９0-9])文法", title)
    if not match:
        return None
    digit = match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return f"N{digit}"


def canonical_payload(title: str, url: str, sections: dict[str, list[str]]) -> str:
    keys = ("接続", "意味", "解説", "例文", "備考")
    lines = [title, url]
    for key in keys:
        lines.append(f"[{key}]")
        lines.extend(sections.get(key, []))
    return "\n".join(lines)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_rag_docs_payload(
    data_dir: Path,
    source_urls: list[str],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for url in source_urls:
        rec = records.get(url)
        if not isinstance(rec, dict):
            continue
        rel_file = str(rec.get("file", "")).strip()
        if not rel_file:
            continue
        article_path = data_dir / rel_file
        article = load_json(article_path, default={})
        if not article:
            continue
        title = str(article.get("title", "")).strip()
        article_url = str(article.get("url", "")).strip()
        sections_raw = article.get("sections", {})
        sections = sections_raw if isinstance(sections_raw, dict) else {}
        if not title or not article_url:
            continue
        items.append(
            {
                "title": title,
                "url": article_url,
                "level": str(article.get("level", "")).strip(),
                "sections": sections,
            }
        )
    return {
        "generated_at": utc_now_iso(),
        "total_items": len(items),
        "items": items,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Japanese grammar articles from mainichi-nonbiri incrementally."
    )
    parser.add_argument(
        "--data-dir",
        default="data/crawl",
        help="Output data directory (default: data/crawl)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Maximum number of URLs to crawl (0 means all).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay seconds between article requests (default: 0.1).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite article files even if unchanged.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-URL progress logs.",
    )
    parser.set_defaults(new_only=True)
    parser.add_argument(
        "--new-only",
        dest="new_only",
        action="store_true",
        help="Only crawl URLs not seen before in manifest (default).",
    )
    parser.add_argument(
        "--full-check",
        dest="new_only",
        action="store_false",
        help="Crawl all URLs in source and compare hash (re-check all).",
    )
    return parser


def sync(
    data_dir: Path,
    max_urls: int,
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

    urls = fetch_grammar_urls()
    if max_urls > 0:
        urls = urls[:max_urls]
    source_urls_set = set(urls)
    crawl_urls = [url for url in urls if url not in existing_articles] if new_only else urls

    summary: dict[str, Any] = {
        "started_at": started_at,
        "source_index_url": GRAMMAR_INDEX_URL,
        "mode": "new-only" if new_only else "full-check",
        "total_urls_in_source": len(urls),
        "total_urls_crawled": len(crawl_urls),
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "removed_from_source": 0,
        "failed_urls": [],
    }

    for i, url in enumerate(crawl_urls, start=1):
        if verbose:
            print(f"[{i}/{len(crawl_urls)}] {url}", flush=True)
        now = utc_now_iso()
        try:
            article = fetch_article(url)
            payload = canonical_payload(article.title, article.url, article.sections)
            digest = sha256_hex(payload)

            prev = existing_articles.get(url) or {}
            prev_hash = prev.get("content_hash")
            status = "unchanged"
            if prev_hash is None:
                status = "new"
            elif prev_hash != digest:
                status = "updated"

            slug = slug_from_url(url)
            rel_file = f"articles/{slug}.json"
            abs_file = data_dir / rel_file

            if force or status in {"new", "updated"} or not abs_file.exists():
                article_json = {
                    "url": url,
                    "title": article.title,
                    "level": normalize_level(article.title),
                    "sections": article.sections,
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
                "url": url,
                "title": article.title,
                "level": normalize_level(article.title),
                "content_hash": digest,
                "file": rel_file,
                "first_seen_at": first_seen_at,
                "last_seen_at": now,
                "last_changed_at": last_changed_at,
                "in_source": True,
            }
            existing_articles[url] = rec
        except Exception as exc:  # pragma: no cover
            summary["failed"] += 1
            summary["failed_urls"].append({"url": url, "error": str(exc)})
            if verbose:
                print(f"  -> failed: {exc}", flush=True)
        finally:
            if delay_sec > 0 and i < len(crawl_urls):
                time.sleep(delay_sec)

    removed = 0
    for url, rec in existing_articles.items():
        if not isinstance(rec, dict):
            continue
        if url in source_urls_set:
            rec["in_source"] = True
            rec["last_seen_at"] = utc_now_iso()
        if not rec.get("in_source", False):
            removed += 1
    summary["removed_from_source"] = removed
    summary["finished_at"] = utc_now_iso()
    index_records = [existing_articles[url] for url in urls if url in existing_articles]

    output_manifest = {
        "source_index_url": GRAMMAR_INDEX_URL,
        "last_sync_at": summary["finished_at"],
        "total_articles_known": len(existing_articles),
        "articles": existing_articles,
    }
    rag_docs_payload = build_rag_docs_payload(
        data_dir=data_dir,
        source_urls=urls,
        records=existing_articles,
    )
    summary["rag_docs_items"] = int(rag_docs_payload.get("total_items", 0))
    dump_json(manifest_path, output_manifest)
    dump_json(index_path, {"generated_at": summary["finished_at"], "items": index_records})
    dump_json(data_dir / "rag_docs.json", rag_docs_payload)
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
        max_urls=max(args.max, 0),
        delay_sec=max(args.delay, 0.0),
        force=bool(args.force),
        verbose=bool(args.verbose),
        new_only=bool(args.new_only),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
