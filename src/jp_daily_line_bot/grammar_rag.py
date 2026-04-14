from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .zh_tw import to_zh_tw

_STOP_WORDS = {
    "する",
    "した",
    "して",
    "です",
    "ます",
    "したい",
    "いる",
    "ある",
    "こと",
    "もの",
    "よう",
    "ため",
    "など",
    "これ",
    "それ",
    "あれ",
    "ここ",
    "そこ",
    "そして",
    "しかし",
    "から",
    "まで",
    "ので",
    "のに",
    "へ",
    "を",
    "は",
    "が",
    "に",
    "で",
    "と",
    "も",
}


@dataclass(frozen=True)
class GrammarDoc:
    title: str
    url: str
    level: str | None
    patterns: tuple[str, ...]
    meaning: str
    explanation: str
    example: str
    example_kanji: tuple[tuple[str, str], ...]
    tokens: frozenset[str]
    trigrams: frozenset[str]


@dataclass(frozen=True)
class GrammarMatch:
    pattern: str
    title: str
    level: str | None
    url: str
    meaning: str
    explanation: str
    example: str
    example_kanji: tuple[tuple[str, str], ...]
    score: float
    matched_terms: tuple[str, ...]


def retrieve_grammar_references(
    data_dir: Path,
    article_title: str,
    article_paragraphs: list[str],
    top_k: int = 3,
) -> list[dict[str, str]]:
    articles_dir = data_dir / "crawl" / "articles"
    docs = _load_docs_cached(str(articles_dir.resolve()))
    if not docs:
        return []

    query_text = "\n".join([article_title] + article_paragraphs)
    query_tokens = set(_tokenize(query_text))
    query_trigrams = _char_trigrams(_normalize(query_text))
    normalized_query = _normalize_pattern_surface(query_text)

    scored: list[GrammarMatch] = []
    for doc in docs:
        overlap = sorted(query_tokens & set(doc.tokens))
        sparse_score = float(len(overlap))

        trigram_score = _jaccard(query_trigrams, set(doc.trigrams))

        pattern_hits = 0
        for p in doc.patterns:
            surface = _normalize_pattern_surface(p)
            if len(surface) < 2:
                continue
            if surface in normalized_query:
                pattern_hits += 1

        score = sparse_score * 1.8 + trigram_score * 6.0 + pattern_hits * 2.5
        if score < 1.5:
            continue

        scored.append(
            GrammarMatch(
                pattern=doc.patterns[0] if doc.patterns else doc.title,
                title=doc.title,
                level=doc.level,
                url=doc.url,
                meaning=doc.meaning,
                explanation=doc.explanation,
                example=doc.example,
                example_kanji=doc.example_kanji,
                score=score,
                matched_terms=tuple(overlap[:4]),
            )
        )

    scored.sort(key=lambda x: x.score, reverse=True)

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in scored:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        results.append(
            {
                "pattern": item.pattern,
                "title": item.title,
                "level": item.level or "",
                "url": item.url,
                "meaning": to_zh_tw(item.meaning),
                "explanation": to_zh_tw(item.explanation),
                "example": item.example,
                "example_kanji": _format_kanji_pairs(item.example_kanji),
                "matched_terms": "、".join(item.matched_terms),
                "score": f"{item.score:.2f}",
            }
        )
        if len(results) >= max(1, top_k):
            break

    return results


@lru_cache(maxsize=2)
def _load_docs_cached(articles_dir_str: str) -> tuple[GrammarDoc, ...]:
    articles_dir = Path(articles_dir_str)
    if not articles_dir.exists():
        return tuple()

    docs: list[GrammarDoc] = []
    for path in sorted(articles_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        title = str(payload.get("title", "")).strip()
        url = str(payload.get("url", "")).strip()
        level = str(payload.get("level", "")).strip() or None
        sections = payload.get("sections", {})
        if not isinstance(sections, dict):
            sections = {}

        meanings = _as_lines(sections.get("意味"))
        explanations = _as_lines(sections.get("解説"))
        examples = _as_lines(sections.get("例文"))
        connections = _as_lines(sections.get("接続"))

        meaning = " / ".join(meanings[:2])
        explanation = " ".join(explanations[:1])
        example = _extract_example(examples)
        reading_pairs = _extract_reading_pairs(title, sections)
        example_kanji = tuple(_select_example_kanji_pairs(example, reading_pairs))

        patterns = _extract_patterns_from_title(title)
        search_text = "\n".join(
            [
                title,
                " ".join(patterns),
                " ".join(connections[:1]),
                meaning,
                explanation,
                " ".join(examples[:1]),
            ]
        )
        normalized = _normalize(search_text)
        tokens = frozenset(_tokenize(normalized))
        trigrams = frozenset(_char_trigrams(normalized))
        if not tokens and not trigrams:
            continue

        docs.append(
            GrammarDoc(
                title=title,
                url=url,
                level=level,
                patterns=tuple(patterns),
                meaning=meaning,
                explanation=explanation,
                example=example,
                example_kanji=example_kanji,
                tokens=tokens,
                trigrams=trigrams,
            )
        )

    return tuple(docs)


def _as_lines(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _extract_patterns_from_title(title: str) -> list[str]:
    cleaned = re.sub(r"^【[^】]*】", "", title).strip()
    if not cleaned:
        return [title]

    parts = re.split(r"[／/、，]\s*", cleaned)
    patterns: list[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if "～" in p or "〜" in p or len(p) <= 12:
            patterns.append(p)
    return patterns or [cleaned]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[0-9０-９a-zA-Z一-龯々ヶぁ-んァ-ンー]+", text)
    out: list[str] = []
    for token in tokens:
        t = token.strip().lower()
        if len(t) < 2:
            continue
        if t in _STOP_WORDS:
            continue
        out.append(t)
    return out


def _char_trigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[i : i + 3] for i in range(len(compact) - 2)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _normalize_pattern_surface(text: str) -> str:
    return re.sub(r"[〜～\s／/・、，。,.！!？?()（）【】\[\]{}]", "", text)


def _extract_example(examples: list[str]) -> str:
    if not examples:
        return ""
    raw = examples[0]
    chunks = [seg.strip() for seg in re.split(r"（\d+）", raw) if seg.strip()]
    for chunk in chunks:
        ja = chunk.split("▶", 1)[0].strip()
        if not ja:
            continue
        if re.search(r"[ぁ-んァ-ン一-龯]", ja):
            return re.sub(r"\s+", " ", ja).strip()
    fallback = raw.split("▶", 1)[0].strip()
    return re.sub(r"\s+", " ", fallback).strip()


def _extract_reading_pairs(title: str, sections: dict[str, object]) -> list[tuple[str, str]]:
    texts: list[str] = [title]
    for value in sections.values():
        if isinstance(value, list):
            texts.extend(str(item) for item in value)

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        for kanji, reading in re.findall(r"([0-9０-９一-龯々ヶぁ-んァ-ンー]+)[(（]([ぁ-んァ-ンー]+)[)）]", text):
            k = re.sub(r"\s+", "", kanji).strip()
            r = re.sub(r"\s+", "", reading).strip()
            if not k or not r:
                continue
            if len(r) < 2:
                continue
            if not re.search(r"[一-龯々ヶ]", k):
                continue
            pair = (k, r)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _select_example_kanji_pairs(example: str, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not example or not pairs:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kanji, reading in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if kanji not in example:
            continue
        pair = (kanji, reading)
        if pair in seen:
            continue
        if any(
            kanji == picked_kanji
            or kanji in picked_kanji
            or picked_kanji in kanji
            for picked_kanji, _ in out
        ):
            continue
        seen.add(pair)
        out.append(pair)
        if len(out) >= 8:
            break
    return out


def _format_kanji_pairs(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    return "、".join(f"{k}={r}" for k, r in pairs)
