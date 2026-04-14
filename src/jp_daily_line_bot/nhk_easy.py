from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, Tag

EASY_BASE_URL = "https://news.web.nhk/news/easy"
EASY_INDEX_URL = f"{EASY_BASE_URL}/"
TOKEN_BOOTSTRAP_URL = "https://www.web.nhk/tix/build_authorize"
TOP_LIST_URL = f"{EASY_BASE_URL}/top-list.json"

DEFAULT_CONSENT = {
    "status": "optedin",
    "entity": "household",
    "area": {
        "areaId": "130",
        "jisx0402": "13101",
        "postal": "1000001",
        "pref": "13",
    },
}


@dataclass
class EasyArticle:
    news_id: str
    title: str
    url: str
    body_html: str
    paragraphs_plain: list[str]
    paragraphs_with_furigana: list[str]
    regular_news_url: str | None


def _clean_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


def _build_consent_cookie_value(consent: dict[str, Any] | None = None) -> str:
    payload = consent or DEFAULT_CONSENT
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return quote(raw, safe="")


def bootstrap_anonymous_session(
    timeout_sec: int = 20,
    consent: dict[str, Any] | None = None,
) -> tuple[requests.Session, str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "jp-daily-line-bot/1.0",
            "Accept-Language": "ja,en;q=0.8",
        }
    )
    session.cookies.set(
        "consentToUse",
        _build_consent_cookie_value(consent),
        domain="web.nhk",
        path="/",
    )

    response = session.get(
        TOKEN_BOOTSTRAP_URL,
        params={
            "idp": "r-alaz",
            "profileType": "anonymous",
            "redirect_uri": EASY_INDEX_URL,
        },
        timeout=timeout_sec,
        allow_redirects=True,
    )
    response.raise_for_status()

    token = session.cookies.get("z_at") or session.cookies.get("z_at", domain=".web.nhk")
    if not token:
        raise RuntimeError("Failed to obtain NHK anonymous token (z_at cookie missing).")
    return session, token


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fetch_top_news_list(session: requests.Session, token: str, timeout_sec: int = 20) -> list[dict[str, Any]]:
    response = session.get(TOP_LIST_URL, headers=_auth_headers(token), timeout=timeout_sec)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected top-list payload.")
    return [item for item in data if isinstance(item, dict)]


def article_url(news_id: str) -> str:
    return f"{EASY_BASE_URL}/{news_id}/{news_id}.html"


def _extract_plain_text(node: Tag) -> str:
    parsed = BeautifulSoup(str(node), "html.parser")
    for rt in parsed.find_all("rt"):
        rt.decompose()
    for ruby in parsed.find_all("ruby"):
        ruby.unwrap()
    return _clean_text(parsed.get_text("", strip=True))


def _extract_furigana_text(node: Tag) -> str:
    parsed = BeautifulSoup(str(node), "html.parser")
    for rt in parsed.find_all("rt"):
        reading = _clean_text(rt.get_text("", strip=True))
        rt.replace_with(f"({reading})")
    for ruby in parsed.find_all("ruby"):
        ruby.unwrap()
    return _clean_text(parsed.get_text("", strip=True))


def fetch_article(session: requests.Session, token: str, news_id: str, timeout_sec: int = 20) -> EasyArticle:
    url = article_url(news_id)
    response = session.get(url, headers=_auth_headers(token), timeout=timeout_sec)
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    body = soup.select_one("#js-article-body")
    if body is None:
        raise ValueError(f"Article body not found for {news_id}.")

    title_node = soup.select_one("h1.article-main__title") or soup.find("h1")
    title = _clean_text(title_node.get_text("", strip=True)) if title_node else news_id

    paragraphs_plain: list[str] = []
    paragraphs_with_furigana: list[str] = []
    for p in body.select("p"):
        plain = _extract_plain_text(p)
        with_furi = _extract_furigana_text(p)
        if plain:
            paragraphs_plain.append(plain)
            paragraphs_with_furigana.append(with_furi or plain)

    if not paragraphs_plain:
        raise ValueError(f"No paragraphs found for {news_id}.")

    regular_news_link = soup.select_one("#js-regular-news[href]")
    regular_news_url = None
    if regular_news_link is not None:
        href = str(regular_news_link.get("href", "")).strip()
        regular_news_url = href or None

    return EasyArticle(
        news_id=news_id,
        title=title,
        url=url,
        body_html=str(body),
        paragraphs_plain=paragraphs_plain,
        paragraphs_with_furigana=paragraphs_with_furigana,
        regular_news_url=regular_news_url,
    )
