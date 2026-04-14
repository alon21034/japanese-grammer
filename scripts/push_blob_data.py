#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from vercel.blob import BlobClient
except Exception as exc:  # pragma: no cover
    BlobClient = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload local NHK/grammar datasets to Vercel Blob."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Local project data directory (default: data).",
    )
    parser.add_argument(
        "--blob-prefix",
        default="",
        help="Optional prefix in Blob store (example: prod or staging).",
    )
    parser.add_argument(
        "--max-nhk-articles",
        type=int,
        default=0,
        help="Upload at most N NHK article JSON files (0 means all).",
    )
    parser.add_argument(
        "--access",
        choices=("public", "private"),
        default="public",
        help="Blob access for uploaded objects (default: public).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without writing.",
    )
    return parser


def _blob_path(prefix: str, rel_path: str) -> str:
    p = prefix.strip().strip("/")
    r = rel_path.strip().lstrip("/")
    return f"{p}/{r}" if p else r


def _iter_upload_targets(data_dir: Path, max_nhk_articles: int) -> list[tuple[Path, str]]:
    targets: list[tuple[Path, str]] = []

    nhk_index = data_dir / "nhk_easy" / "index.json"
    if nhk_index.exists():
        targets.append((nhk_index, "nhk_easy/index.json"))

    nhk_articles_dir = data_dir / "nhk_easy" / "articles"
    if nhk_articles_dir.exists():
        article_paths = sorted(nhk_articles_dir.glob("*.json"))
        if max_nhk_articles > 0:
            article_paths = article_paths[:max_nhk_articles]
        for path in article_paths:
            targets.append((path, f"nhk_easy/articles/{path.name}"))

    rag_docs = data_dir / "crawl" / "rag_docs.json"
    if rag_docs.exists():
        targets.append((rag_docs, "crawl/rag_docs.json"))

    return targets


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if BlobClient is None and not args.dry_run:
        print(
            f"Failed to import vercel blob SDK: {IMPORT_ERROR}\n"
            "Please install dependency first: pip install vercel",
            file=sys.stderr,
        )
        return 2

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()

    targets = _iter_upload_targets(
        data_dir=data_dir,
        max_nhk_articles=max(args.max_nhk_articles, 0),
    )
    if not targets:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "No local dataset files found. Run sync scripts first.",
                    "data_dir": str(data_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    uploaded = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    client = None if args.dry_run else BlobClient()

    for local_path, rel_blob_path in targets:
        path_in_blob = _blob_path(args.blob_prefix, rel_blob_path)
        if args.dry_run:
            print(f"[dry-run] {local_path} -> {path_in_blob}")
            skipped += 1
            continue

        try:
            body = local_path.read_bytes()
            client.put(
                path_in_blob,
                body,
                access=args.access,
                content_type="application/json; charset=utf-8",
                add_random_suffix=False,
                overwrite=True,
            )
            uploaded += 1
        except Exception as exc:  # pragma: no cover
            errors.append({"file": str(local_path), "blob_path": path_in_blob, "error": str(exc)})

    summary = {
        "ok": len(errors) == 0,
        "uploaded": uploaded,
        "dry_run": bool(args.dry_run),
        "skipped": skipped,
        "errors": errors,
        "data_dir": str(data_dir),
        "blob_prefix": args.blob_prefix.strip("/"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
