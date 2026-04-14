from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://mainichi-nonbiri.com"
GRAMMAR_INDEX_URL = f"{BASE_URL}/japanese-grammar/"
GRAMMAR_PATH_PREFIX = "/grammar/"
TARGET_SECTIONS = ("接続", "意味", "解説", "例文", "備考")


@dataclass
class GrammarArticle:
    title: str
    url: str
    sections: dict[str, list[str]]


def _clean_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_valid_grammar_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc == "mainichi-nonbiri.com"
        and parsed.path.startswith(GRAMMAR_PATH_PREFIX)
        and parsed.path != GRAMMAR_PATH_PREFIX
    )


def fetch_grammar_urls(timeout_sec: int = 20) -> list[str]:
    response = requests.get(GRAMMAR_INDEX_URL, timeout=timeout_sec)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    seen: set[str] = set()
    ordered: list[str] = []
    for link in soup.select("a[href]"):
        raw_href = link["href"]
        normalized = urljoin(BASE_URL, raw_href)
        if not _is_valid_grammar_url(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _iter_content_nodes(article_root: Tag) -> Iterable[Tag]:
    for node in article_root.descendants:
        if isinstance(node, Tag):
            yield node


def fetch_article(url: str, timeout_sec: int = 20) -> GrammarArticle:
    response = requests.get(url, timeout=timeout_sec)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    article_root = soup.find("article") or soup.find("main") or soup.body
    if article_root is None:
        raise ValueError("Cannot find article body.")

    h1 = article_root.find("h1")
    title = _clean_text(h1.get_text(" ", strip=True)) if h1 else "日本語文法"

    sections: dict[str, list[str]] = {key: [] for key in TARGET_SECTIONS}
    current_section: str | None = None

    for node in _iter_content_nodes(article_root):
        if node.name in {"h2", "h3", "h4"}:
            heading_text = _clean_text(node.get_text(" ", strip=True))
            matched = next((key for key in TARGET_SECTIONS if key in heading_text), None)
            if matched:
                current_section = matched
            elif any(stop in heading_text for stop in ("関連記事", "コメント", "目次")):
                current_section = None
            continue

        if current_section is None:
            continue

        if node.name not in {"p", "li"}:
            continue

        text = _clean_text(node.get_text(" ", strip=True))
        if not text or text == "Image":
            continue
        sections[current_section].append(text)

    return GrammarArticle(title=title, url=url, sections=sections)


def choose_next_article(urls: list[str], sent_urls: list[str]) -> str:
    remaining = [url for url in urls if url not in set(sent_urls)]
    if remaining:
        return remaining[0]
    return urls[0]

