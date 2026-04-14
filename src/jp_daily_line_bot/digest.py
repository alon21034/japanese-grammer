from __future__ import annotations

import re

from .mainichi import GrammarArticle


def _take(lines: list[str], n: int) -> list[str]:
    return [line for line in lines[:n] if line]


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _is_japanese_sentence(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龯]", text))


def format_digest_message(article: GrammarArticle) -> str:
    sec = article.sections
    connections = _take(sec.get("接続", []), 2)
    meanings = _take(sec.get("意味", []), 3)
    explanations = _take(sec.get("解説", []), 1)
    examples = [line for line in sec.get("例文", []) if _is_japanese_sentence(line)]
    examples = _take(examples, 2)

    lines: list[str] = []
    lines.append("【今日文法】")
    lines.append(article.title)
    lines.append("")

    if connections:
        lines.append("接続")
        lines.extend(f"- {item}" for item in connections)
        lines.append("")

    if meanings:
        lines.append("意思")
        lines.extend(f"- {item}" for item in meanings)
        lines.append("")

    if explanations:
        lines.append("解説重點")
        lines.append(f"- {_truncate(explanations[0], 170)}")
        lines.append("")

    if examples:
        lines.append("例文")
        lines.extend(f"- {_truncate(item, 120)}" for item in examples)
        lines.append("")

    lines.append(f"原文：{article.url}")
    message = "\n".join(lines).strip()

    # LINE text message max length is 5000 chars.
    return _truncate(message, 4900)

