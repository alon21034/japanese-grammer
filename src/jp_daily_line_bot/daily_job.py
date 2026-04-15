from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_offline import build_detailed_explanations_offline
from .config import load_settings
from .grammar_rag import retrieve_grammar_references
from .line_api import LineClient
from .nhk_easy import bootstrap_anonymous_session, fetch_article, fetch_top_news_list
from .nhk_lesson import (
    build_fallback_lesson,
    choose_next_news,
    format_lesson_messages,
    format_push_digest_message,
    GeneratedLesson,
    NHKEasyArticle,
    load_nhk_article,
    load_nhk_index,
    pending_quiz_payload,
)
from .storage import nhk_progress_store, quiz_state_store, subscribers_store
from .unified_analysis import build_unified_analysis


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _bootstrap_nhk_seed_data() -> None:
    settings = load_settings()
    base = settings.data_dir / "nhk_easy"
    index_path = base / "index.json"
    articles_dir = base / "articles"

    session, token = bootstrap_anonymous_session()
    top_list = fetch_top_news_list(session, token)
    first = next((item for item in top_list if str(item.get("news_id", "")).strip()), None)
    if not isinstance(first, dict):
        raise RuntimeError("NHK top-list is empty.")

    news_id = str(first.get("news_id", "")).strip()
    article = fetch_article(session, token, news_id)
    grammar_references = retrieve_grammar_references(
        settings.data_dir,
        article_title=article.title,
        article_paragraphs=article.paragraphs_plain,
        top_k=3,
    )
    offline_detailed_explanations = build_detailed_explanations_offline(
        article_title=article.title,
        article_paragraphs=article.paragraphs_plain,
        grammar_references=grammar_references,
    )
    unified_analysis = build_unified_analysis(
        news_id=news_id,
        published_at=str(first.get("news_prearranged_time", "")).strip() or None,
        url=article.url,
        article_sentences=article.paragraphs_plain,
        grammar_candidates=grammar_references,
    )

    article_path = articles_dir / f"{news_id}.json"
    _dump_json(
        article_path,
        {
            "news_id": news_id,
            "title": article.title,
            "url": article.url,
            "published_at": str(first.get("news_prearranged_time", "")).strip() or None,
            "regular_news_url": article.regular_news_url,
            "paragraphs_plain": article.paragraphs_plain,
            "paragraphs_with_furigana": article.paragraphs_with_furigana,
            "grammar_references": grammar_references,
            "offline_detailed_explanations": offline_detailed_explanations,
            "unified_analysis": unified_analysis,
            "body_html": article.body_html,
            "top_list_item": first,
            "scraped_at": utc_now_iso(),
        },
    )
    _dump_json(
        index_path,
        {
            "generated_at": utc_now_iso(),
            "items": [
                {
                    "news_id": news_id,
                    "title": article.title,
                    "url": article.url,
                    "file": f"articles/{news_id}.json",
                }
            ],
        },
    )


def _subscriber_ids() -> list[str]:
    settings = load_settings()
    sub_store = subscribers_store(settings.data_dir)
    sub_data = sub_store.load()
    subscribers = [str(uid) for uid in sub_data.get("user_ids", []) if uid]
    if settings.line_user_id and settings.line_user_id not in subscribers:
        subscribers.append(settings.line_user_id)
    if settings.local_test_mode and settings.local_test_user_id not in subscribers:
        subscribers.append(settings.local_test_user_id)
    return subscribers


def _save_pending_quiz(user_ids: list[str], quiz_payload: dict[str, Any]) -> None:
    settings = load_settings()
    store = quiz_state_store(settings.data_dir)
    data = store.load()
    users = data.get("users", {})
    if not isinstance(users, dict):
        users = {}
    saved_at = utc_now_iso()
    for user_id in user_ids:
        users[user_id] = {**quiz_payload, "saved_at": saved_at}
    store.save({"users": users})


def _sent_news_ids() -> list[str]:
    settings = load_settings()
    prog_data = nhk_progress_store(settings.data_dir).load()
    return [str(news_id) for news_id in prog_data.get("sent_news_ids", []) if news_id]


def mark_news_sent(news_id: str) -> None:
    settings = load_settings()
    prog_store = nhk_progress_store(settings.data_dir)
    sent_news_ids = _sent_news_ids()
    if news_id in sent_news_ids:
        sent_news_ids = [news_id]
    else:
        sent_news_ids.append(news_id)
    prog_store.save({"sent_news_ids": sent_news_ids})


def _build_lesson_components():
    settings = load_settings()
    index_items = load_nhk_index(settings.data_dir)
    if not index_items:
        _bootstrap_nhk_seed_data()
        index_items = load_nhk_index(settings.data_dir)
    if not index_items:
        raise RuntimeError("No NHK Easy articles found.")

    news_id = choose_next_news(index_items, _sent_news_ids())
    article = load_nhk_article(settings.data_dir, news_id)
    grammar_references = retrieve_grammar_references(
        settings.data_dir,
        article_title=article.title,
        article_paragraphs=article.paragraphs_plain,
        top_k=3,
    )

    lesson = _build_offline_lesson(article, grammar_references)
    return news_id, article, lesson, grammar_references


def _build_offline_lesson(article: NHKEasyArticle, grammar_references: list[dict[str, str]]) -> GeneratedLesson:
    fallback = build_fallback_lesson(article)
    if not grammar_references:
        return fallback

    grammar_points: list[dict[str, str]] = []
    for ref in grammar_references[:3]:
        pattern = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        meaning = str(ref.get("meaning", "")).strip() or "文法重點"
        explanation = str(ref.get("explanation", "")).strip() or "此文法在文章中出現，可用來理解句子語氣與結構。"
        if not pattern:
            continue
        article_example = ""
        normalized_pattern = pattern.replace(" ", "")
        for sentence in article.paragraphs_plain:
            if normalized_pattern and normalized_pattern in sentence.replace(" ", ""):
                article_example = sentence
                break
        if not article_example:
            article_example = str(ref.get("example", "")).strip() or article.paragraphs_plain[0]

        grammar_points.append(
            {
                "pattern": pattern,
                "meaning_zh": meaning,
                "explanation_zh": explanation,
                "example_from_article": article_example,
            }
        )

    if not grammar_points:
        return fallback
    return GeneratedLesson(grammar_points=grammar_points, questions=fallback.questions)


def build_daily_lesson(include_furigana: bool = False) -> tuple[str, list[str], dict[str, Any]]:
    news_id, article, lesson, grammar_references = _build_lesson_components()
    messages = format_lesson_messages(
        article,
        lesson,
        include_furigana=include_furigana,
        grammar_references=grammar_references,
    )
    return news_id, messages, pending_quiz_payload(article, lesson, include_furigana=include_furigana)


def build_line_digest(include_furigana: bool = False) -> tuple[str, str]:
    news_id, article, lesson, grammar_references = _build_lesson_components()
    message = format_push_digest_message(
        article,
        lesson,
        include_furigana=include_furigana,
        grammar_references=grammar_references,
    )
    return news_id, message


def run_daily_push() -> str:
    settings = load_settings()
    line_client = LineClient(
        channel_access_token=settings.line_channel_access_token,
        channel_secret=settings.line_channel_secret,
        local_test_mode=settings.local_test_mode,
        local_test_log_path=settings.data_dir / "line_mock_events.jsonl",
    )
    if not line_client.is_configured:
        raise RuntimeError("LINE credentials missing. Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.")

    subscribers = _subscriber_ids()
    if not subscribers:
        raise RuntimeError("No subscribers found. Add LINE_USER_ID or register via webhook first.")

    news_id, message = build_line_digest(include_furigana=False)
    for user_id in subscribers:
        line_client.push_text(user_id, message)
    mark_news_sent(news_id)
    return news_id


def build_lesson_for_user(user_id: str, include_furigana: bool = False) -> tuple[str, list[str]]:
    news_id, messages, quiz_payload = build_daily_lesson(include_furigana=include_furigana)
    _save_pending_quiz([user_id], quiz_payload)
    return news_id, messages


def build_line_digest_for_user(user_id: str, include_furigana: bool = False) -> tuple[str, str]:
    _ = user_id
    return build_line_digest(include_furigana=include_furigana)


def build_local_reading_payload(include_furigana: bool = False) -> dict[str, Any]:
    news_id, article, lesson, grammar_references = _build_lesson_components()
    sentences = article.paragraphs_plain
    unified_analysis = article.unified_analysis if isinstance(article.unified_analysis, dict) else {}
    if not unified_analysis:
        unified_analysis = build_unified_analysis(
            news_id=news_id,
            published_at=article.published_at,
            url=article.url,
            article_sentences=sentences,
            grammar_candidates=grammar_references[:3],
        )
    return {
        "news_id": news_id,
        "title": article.title,
        "url": article.url,
        "include_furigana": include_furigana,
        "sentences": sentences,
        "sentences_furigana": article.paragraphs_with_furigana,
        "grammar_points": lesson.grammar_points[:3],
        "grammar_references": grammar_references[:3],
        "offline_detailed_explanations": article.offline_detailed_explanations[:3],
        "unified_analysis": unified_analysis,
    }


if __name__ == "__main__":
    pushed_news_id = run_daily_push()
    print(f"Pushed: {pushed_news_id}")
