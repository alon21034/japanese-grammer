from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import load_settings
from .grammar_rag import retrieve_grammar_references
from .line_api import LineClient
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        raise RuntimeError("No NHK Easy articles found. Run scripts/sync_nhk_easy.py first.")

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
    }


if __name__ == "__main__":
    pushed_news_id = run_daily_push()
    print(f"Pushed: {pushed_news_id}")
