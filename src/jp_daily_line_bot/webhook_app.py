from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import load_settings
from .daily_job import build_line_digest_for_user, build_local_reading_payload, run_daily_push
from .line_api import LineClient
from .nhk_lesson import format_question_message, normalize_short_answer, parse_user_answer
from .codex_offline import build_detailed_explanations_offline, build_sentence_explanation_offline
from .storage import (
    grammar_explain_cache_store,
    local_ui_state_store,
    quiz_state_store,
    subscribers_store,
)
from .zh_tw import to_zh_tw

settings = load_settings()
line_client = LineClient(
    channel_access_token=settings.line_channel_access_token,
    channel_secret=settings.line_channel_secret,
    local_test_mode=settings.local_test_mode,
    local_test_log_path=settings.data_dir / "line_mock_events.jsonl",
)
sub_store = subscribers_store(settings.data_dir)
quiz_store = quiz_state_store(settings.data_dir)
local_ui_store = local_ui_state_store(settings.data_dir)
explain_cache_store = grammar_explain_cache_store(settings.data_dir)

app = FastAPI(title="JP Grammar LINE Bot")
LESSON_TRIGGERS = {"今日文法", "today", "now"}
EXPLAIN_CACHE_VERSION = "v3"
EXPLAIN_CACHE_MAX_ITEMS = 200


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _cron_authorized(authorization: str | None) -> bool:
    expected = os.getenv("CRON_SECRET", "").strip()
    if not expected:
        return True
    return (authorization or "").strip() == f"Bearer {expected}"


@app.get("/internal/cron/daily-push")
@app.post("/internal/cron/daily-push")
def cron_daily_push(authorization: str | None = Header(default=None)) -> JSONResponse:
    if not _cron_authorized(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized cron request.")
    try:
        news_id = run_daily_push()
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "news_id": news_id})


def _add_subscriber(user_id: str) -> bool:
    data = sub_store.load()
    user_ids = [str(uid) for uid in data.get("user_ids", []) if uid]
    if user_id in user_ids:
        return False
    user_ids.append(user_id)
    sub_store.save({"user_ids": user_ids})
    return True


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _resolve_user_id(user_id: str | None) -> str:
    candidate = (user_id or "").strip()
    if candidate:
        return candidate
    return settings.local_test_user_id


def _load_local_ui_users() -> dict[str, dict]:
    data = local_ui_store.load()
    users = data.get("users", {})
    if not isinstance(users, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in users.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def _save_local_ui_users(users: dict[str, dict]) -> None:
    local_ui_store.save({"users": users})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_refs_for_cache(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ref_raw in refs[:3]:
        if not isinstance(ref_raw, dict):
            continue
        ref = {
            "pattern": str(ref_raw.get("pattern", "")).strip() or str(ref_raw.get("title", "")).strip(),
            "level": str(ref_raw.get("level", "")).strip(),
            "meaning": str(ref_raw.get("meaning", "")).strip(),
            "explanation": str(ref_raw.get("explanation", "")).strip(),
            "example": str(ref_raw.get("example", "")).strip(),
            "example_kanji": str(ref_raw.get("example_kanji", "")).strip(),
            "url": str(ref_raw.get("url", "")).strip(),
        }
        if not ref["pattern"]:
            continue
        out.append(ref)
    return out


def _grammar_refs(session: dict[str, Any]) -> list[dict[str, Any]]:
    refs_raw = session.get("grammar_references", [])
    refs = refs_raw if isinstance(refs_raw, list) else []
    out: list[dict[str, Any]] = []
    for ref_raw in refs[:3]:
        if not isinstance(ref_raw, dict):
            continue
        pattern = str(ref_raw.get("pattern", "")).strip() or str(ref_raw.get("title", "")).strip()
        if not pattern:
            continue
        out.append(ref_raw)
    return out


def _current_grammar_index(session: dict[str, Any], refs: list[dict[str, Any]]) -> int:
    if not refs:
        return 0
    raw = int(session.get("grammar_index", 0))
    return max(0, min(raw, len(refs) - 1))


def _build_explain_cache_key(refs: list[dict[str, str]]) -> str:
    payload = {
        "version": EXPLAIN_CACHE_VERSION,
        "refs": refs,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_explain_cache_items() -> dict[str, dict[str, Any]]:
    data = explain_cache_store.load()
    items = data.get("items", {})
    if not isinstance(items, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in items.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def _save_explain_cache_items(items: dict[str, dict[str, Any]]) -> None:
    explain_cache_store.save({"items": items})


def _save_explain_cache_entry(
    cache_key: str,
    refs: list[dict[str, str]],
    details: list[dict[str, str]],
) -> None:
    items = _load_explain_cache_items()
    items[cache_key] = {
        "saved_at": _utc_now_iso(),
        "version": EXPLAIN_CACHE_VERSION,
        "refs": refs,
        "details": details,
    }
    if len(items) > EXPLAIN_CACHE_MAX_ITEMS:
        sorted_keys = sorted(
            items.keys(),
            key=lambda k: str(items.get(k, {}).get("saved_at", "")),
        )
        drop_count = len(items) - EXPLAIN_CACHE_MAX_ITEMS
        for key in sorted_keys[:drop_count]:
            items.pop(key, None)
    _save_explain_cache_items(items)


def _format_sentence_message(session: dict[str, Any]) -> str:
    url = str(session.get("url", "")).strip()
    sentences_raw = session.get("sentences", [])
    sentences = sentences_raw if isinstance(sentences_raw, list) else []
    index = int(session.get("index", 0))
    if not sentences:
        return "目前沒有可顯示的句子。"
    index = max(0, min(index, len(sentences) - 1))
    sentence = str(sentences[index]).strip()
    lines = [f"【句子 {index + 1}/{len(sentences)}】"]
    lines.append(_truncate(sentence, 260))

    furi_lines_raw = session.get("sentences_furigana", [])
    furi_lines = furi_lines_raw if isinstance(furi_lines_raw, list) else []
    furi_line = str(furi_lines[index]).strip() if index < len(furi_lines) else ""
    kanji_pairs = _extract_inline_furigana_pairs(furi_line)
    if kanji_pairs:
        lines.append("")
        lines.append("【漢字標註】")
        for k, r in kanji_pairs[:10]:
            lines.append(f"{k}({r})")

    if url:
        lines.append(f"原文：{_truncate(url, 160)}")
    return _truncate("\n".join(lines), 4900)


def _format_full_article_message(session: dict[str, Any]) -> str:
    sentences_raw = session.get("sentences", [])
    sentences = [str(line).strip() for line in sentences_raw if str(line).strip()] if isinstance(sentences_raw, list) else []
    if not sentences:
        return "目前沒有可顯示的整篇文章。"

    lines = ["【整篇文章】", ""]
    lines.extend(sentences)

    url = str(session.get("url", "")).strip()
    if url:
        lines.extend(["", f"原文：{_truncate(url, 180)}"])

    return _truncate("\n".join(lines), 4900)


def _format_sentence_explanation_message(session: dict[str, Any]) -> str:
    sentences_raw = session.get("sentences", [])
    sentences = sentences_raw if isinstance(sentences_raw, list) else []
    if not sentences:
        return "目前沒有可解釋的句子。"

    index = int(session.get("index", 0))
    index = max(0, min(index, len(sentences) - 1))
    sentence = str(sentences[index]).strip()
    normalized_sentence = re.sub(r"[ 　]", "", sentence)

    refs = _grammar_refs(session)
    matched: list[dict[str, Any]] = []
    for ref in refs[:3]:
        pattern = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        surface = re.sub(r"[ 　〜～・/／\-\.\(\)]+", "", pattern)
        if not surface:
            continue
        if surface in normalized_sentence:
            matched.append(ref)

    if not matched:
        offline = build_sentence_explanation_offline(sentence=sentence, max_points=2)
        lines: list[str] = ["【句子解釋】", f"句子 {index + 1}/{len(sentences)}：{_truncate(sentence, 220)}"]

        chunks_raw = offline.get("chunks", [])
        chunks = chunks_raw if isinstance(chunks_raw, list) else []
        if chunks:
            lines.append("")
            lines.append("【句型拆解】")
            for i, part in enumerate(chunks[:4], start=1):
                lines.append(f"{i}. {_truncate(str(part).strip(), 120)}")

        points_raw = offline.get("grammar_points", [])
        points = points_raw if isinstance(points_raw, list) else []
        if points:
            lines.append("")
            lines.append("【一般文法解釋】")
            for p in points[:2]:
                if not isinstance(p, dict):
                    continue
                pattern = _truncate(str(p.get("pattern", "")).strip(), 40)
                meaning = _truncate(to_zh_tw(str(p.get("meaning_zh", "")).strip()), 70)
                explanation = _truncate(to_zh_tw(str(p.get("explanation_zh", "")).strip()), 120)
                snippet = _truncate(str(p.get("snippet", "")).strip(), 90)
                if pattern:
                    line = f"- {pattern}"
                    if meaning:
                        line += f"：{meaning}"
                    lines.append(line)
                if explanation:
                    lines.append(f"  用法：{explanation}")
                if snippet:
                    lines.append(f"  本句片段：{snippet}")

        steps_raw = offline.get("steps", [])
        steps = steps_raw if isinstance(steps_raw, list) else []
        if steps:
            lines.append("")
            lines.append("【理解步驟】")
            for i, step in enumerate(steps[:3], start=1):
                lines.append(f"{i}. {_truncate(to_zh_tw(str(step).strip()), 100)}")

        furi_lines_raw = session.get("sentences_furigana", [])
        furi_lines = furi_lines_raw if isinstance(furi_lines_raw, list) else []
        furi_line = str(furi_lines[index]).strip() if index < len(furi_lines) else ""
        kanji_pairs = _extract_inline_furigana_pairs(furi_line)
        if kanji_pairs:
            lines.append("")
            lines.append("【漢字標註】")
            for k, r in kanji_pairs[:10]:
                lines.append(f"{k}({r})")
        return _truncate("\n".join(lines), 4900)

    lines: list[str] = ["【句子解釋】", f"句子 {index + 1}/{len(sentences)}：{_truncate(sentence, 220)}"]
    lines.append("")
    lines.append("【句型拆解】")
    chunks = [part.strip() for part in re.split(r"[、。]", sentence) if part.strip()]
    if not chunks:
        chunks = [sentence]
    for i, part in enumerate(chunks[:4], start=1):
        lines.append(f"{i}. {_truncate(part, 120)}")

    lines.append("")
    lines.append("【本句對照文法與作用】")
    for ref in matched[:2]:
        pattern_raw = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        pattern = _truncate(pattern_raw, 40)
        meaning = _truncate(to_zh_tw(str(ref.get("meaning", "")).strip()), 70)
        explanation = _truncate(to_zh_tw(str(ref.get("explanation", "")).strip()), 120)
        line = f"- {pattern}"
        if meaning:
            line += f"：{meaning}"
        lines.append(line)
        if explanation:
            lines.append(f"  用法：{explanation}")

        snippet = sentence
        if pattern_raw and pattern_raw in sentence:
            pos = sentence.find(pattern_raw)
            start = max(0, pos - 14)
            end = min(len(sentence), pos + len(pattern_raw) + 14)
            snippet = sentence[start:end]
        lines.append(f"  本句片段：{_truncate(snippet, 80)}")

    lines.append("")
    lines.append("【理解步驟】")
    lines.append("1. 先抓主要動作與句尾，判斷敘述是能力、原因或狀態。")
    lines.append("2. 再看文法前後接續，確認是修飾名詞、連接句子，還是補充語氣。")
    lines.append("3. 最後把拆解片段合併回整句，確認語意是否自然。")

    furi_lines_raw = session.get("sentences_furigana", [])
    furi_lines = furi_lines_raw if isinstance(furi_lines_raw, list) else []
    furi_line = str(furi_lines[index]).strip() if index < len(furi_lines) else ""
    kanji_pairs = _extract_inline_furigana_pairs(furi_line)
    if kanji_pairs:
        lines.append("")
        lines.append("【漢字標註】")
        for k, r in kanji_pairs[:10]:
            lines.append(f"{k}({r})")

    return _truncate("\n".join(lines), 4900)


def _extract_inline_furigana_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kanji, reading in re.findall(r"([0-9０-９一-龯々ヶ]+)\(([^)]+)\)", text):
        k = kanji.strip()
        r = reading.strip()
        if not k or not r:
            continue
        pair = (k, r)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _format_grammar_message(session: dict[str, Any]) -> str:
    refs = _grammar_refs(session)
    if not refs:
        return "目前沒有可搭配的文法點。"

    idx = _current_grammar_index(session, refs)
    ref = refs[idx]
    pattern = _truncate(str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip(), 50)
    level = str(ref.get("level", "")).strip()
    meaning = _truncate(to_zh_tw(str(ref.get("meaning", "")).strip()), 80)
    explanation = _truncate(to_zh_tw(str(ref.get("explanation", "")).strip()), 140)
    example = _truncate(str(ref.get("example", "")).strip(), 120)
    example_kanji = _truncate(str(ref.get("example_kanji", "")).strip(), 140)
    url = _truncate(str(ref.get("url", "")).strip(), 120)

    lines: list[str] = [f"【文法庫對照 {idx + 1}/{len(refs)}】"]
    line = f"{idx + 1}. {pattern}" + (f" [{level}]" if level else "")
    lines.append(line)
    if meaning:
        lines.append(f"- 意思：{meaning}")
    if explanation:
        lines.append(f"- 解釋：{explanation}")
    if example:
        lines.append(f"- 例句（文法庫）：{example}")
    if example_kanji:
        lines.append(f"- 漢字標註：{example_kanji}")
    if url:
        lines.append(f"- 來源：{url}")
    lines.append("")
    lines.append("提示：按「上一句/下一句」可切換文法；文法看完後按「下一句」進入新聞句子。")
    return _truncate("\n".join(lines), 4900)


def _format_detailed_explanation_message(
    session: dict[str, Any],
    details: list[dict[str, str]],
    *,
    warning: str = "",
) -> str:
    refs = _grammar_refs(session)
    if not refs:
        return "目前沒有可搭配的文法點。"
    idx = _current_grammar_index(session, refs)
    ref = refs[idx]

    details_norm: list[dict[str, str]] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        details_norm.append(
            {
                "pattern": str(item.get("pattern", "")).strip(),
                "detailed_explanation_zh": str(item.get("detailed_explanation_zh", "")).strip(),
                "example_breakdown_zh": str(item.get("example_breakdown_zh", "")).strip(),
                "common_confusion_zh": str(item.get("common_confusion_zh", "")).strip(),
            }
        )

    lines: list[str] = ["【文法詳細解釋】"]
    if warning:
        lines.append(f"註記：{_truncate(to_zh_tw(warning), 180)}")
        lines.append("")

    pattern = _truncate(str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip(), 50)
    meaning = _truncate(to_zh_tw(str(ref.get("meaning", "")).strip()), 80)
    explanation = _truncate(to_zh_tw(str(ref.get("explanation", "")).strip()), 140)
    example = _truncate(str(ref.get("example", "")).strip(), 120)
    example_kanji = _truncate(str(ref.get("example_kanji", "")).strip(), 140)
    url = _truncate(str(ref.get("url", "")).strip(), 120)

    lines.append(f"{idx + 1}. {pattern}")
    if meaning:
        lines.append(f"- 意思：{meaning}")
    if example:
        lines.append(f"- 例句（文法庫）：{example}")
    if example_kanji:
        lines.append(f"- 漢字標註：{example_kanji}")

    detail = None
    for candidate in details_norm:
        if candidate.get("pattern") == pattern:
            detail = candidate
            break
    if detail is None and details_norm:
        detail = details_norm[0]

    if detail:
        detailed = _truncate(to_zh_tw(detail.get("detailed_explanation_zh", "")), 260)
        breakdown = _truncate(to_zh_tw(detail.get("example_breakdown_zh", "")), 240)
        confusion = _truncate(to_zh_tw(detail.get("common_confusion_zh", "")), 220)
        if detailed:
            lines.append(f"- 詳細解釋：{detailed}")
        if breakdown:
            lines.append(f"- 例句解析：{breakdown}")
        if confusion:
            lines.append(f"- 易混淆點：{confusion}")
    elif explanation:
        lines.append(f"- 詳細解釋：{explanation}")

    if url:
        lines.append(f"- 來源：{url}")

    return _truncate("\n".join(lines), 4900)


def _find_offline_detail_for_ref(session: dict[str, Any], ref: dict[str, Any]) -> list[dict[str, str]]:
    details_raw = session.get("offline_detailed_explanations", [])
    details = details_raw if isinstance(details_raw, list) else []
    normalized_pattern = (
        str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
    )
    if not normalized_pattern:
        return []

    for item_raw in details:
        if not isinstance(item_raw, dict):
            continue
        pattern = str(item_raw.get("pattern", "")).strip()
        if pattern == normalized_pattern:
            return [item_raw]
    return []


def _is_lesson_trigger(text: str) -> bool:
    normalized = text.strip().lower()
    return (
        normalized in LESSON_TRIGGERS
        or normalized.startswith("今日文法")
        or normalized.startswith("today")
    )


def _wants_furigana(text: str) -> bool:
    keywords = {"ふりがな", "平假名", "假名", "furigana", "hiragana", "kana"}
    return any(keyword.lower() in text.lower() for keyword in keywords)


def _handle_quiz_answer(user_id: str, text: str) -> str | None:
    state = quiz_store.load()
    users = state.get("users", {})
    if not isinstance(users, dict):
        users = {}

    pending = users.get(user_id)
    if not isinstance(pending, dict):
        if parse_user_answer(text) is None:
            return None
        return "目前沒有待回答題目，輸入「今日文法」先拿一題。"

    questions_raw = pending.get("questions", [])
    questions = questions_raw if isinstance(questions_raw, list) else []
    current_index = int(pending.get("current_index", 0))
    if not questions or current_index < 0 or current_index >= len(questions):
        users.pop(user_id, None)
        quiz_store.save({"users": users})
        return "題目狀態已過期，請輸入「今日文法」重新取得。"

    question_raw = questions[current_index]
    question = question_raw if isinstance(question_raw, dict) else {}
    qtype = str(question.get("type", "")).strip().lower()
    accepted_raw = question.get("accepted_answers", [])
    accepted_answers = [str(item).strip() for item in accepted_raw if str(item).strip()] if isinstance(accepted_raw, list) else []
    if qtype not in {"mcq", "short_answer"} or not accepted_answers:
        users.pop(user_id, None)
        quiz_store.save({"users": users})
        return "題目狀態已過期，請輸入「今日文法」重新取得。"

    is_correct = False
    user_display = text.strip()
    answer_line = ""
    if qtype == "mcq":
        user_answer = parse_user_answer(text)
        if user_answer is None:
            return "此題為選擇題，請回覆 A/B/C/D（或 1/2/3/4）。"
        accepted_set = {ans.strip().upper() for ans in accepted_answers}
        is_correct = user_answer in accepted_set
        user_display = user_answer
        options_raw = question.get("options", {})
        options = options_raw if isinstance(options_raw, dict) else {}
        correct_key = accepted_answers[0].strip().upper()
        if correct_key:
            correct_text = str(options.get(correct_key, "")).strip()
            answer_line = f"正解：{correct_key}" + (f". {correct_text}" if correct_text else "")
    else:
        normalized_user = normalize_short_answer(text)
        if not normalized_user:
            return "此題請輸入簡短日文答案。"
        accepted_norm = {normalize_short_answer(ans): ans for ans in accepted_answers}
        is_correct = normalized_user in accepted_norm
        model_answer = accepted_answers[0]
        answer_line = f"參考答案：{model_answer}"

    explanation = to_zh_tw(str(question.get("explanation_zh") or "").strip())
    title = str(pending.get("title", "")).strip()
    url = str(pending.get("url", "")).strip()

    lines: list[str] = []
    lines.append(f"第 {current_index + 1}/{len(questions)} 題")
    if is_correct:
        lines.append(f"答對了，你的答案：{user_display}")
    else:
        lines.append(f"答錯了，你的答案：{user_display}")

    if answer_line:
        lines.append(answer_line)
    if explanation:
        lines.append(f"解析：{_truncate(explanation, 220)}")
    if title:
        lines.append(f"題目來源：{_truncate(title, 80)}")
    if url:
        lines.append(f"原文：{url}")

    next_index = current_index + 1
    if next_index >= len(questions):
        users.pop(user_id, None)
        lines.append("已完成全部題目。")
    else:
        pending["current_index"] = next_index
        users[user_id] = pending
        next_q_raw = questions[next_index]
        next_q = next_q_raw if isinstance(next_q_raw, dict) else {}
        glossary_raw = pending.get("furigana_glossary", {})
        glossary = glossary_raw if isinstance(glossary_raw, dict) else {}
        lines.append("")
        lines.append(
            format_question_message(
                next_q,
                next_index + 1,
                len(questions),
                furigana_glossary={str(k): str(v) for k, v in glossary.items()},
            )
        )

    quiz_store.save({"users": users})
    return _truncate("\n".join(lines), 4900)


class LocalLessonRequest(BaseModel):
    user_id: str | None = None
    include_furigana: bool = False


class LocalAnswerRequest(BaseModel):
    user_id: str | None = None
    answer: str


class LocalStepRequest(BaseModel):
    user_id: str | None = None


LOCAL_UI_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NHK Grammar Local UI</title>
  <style>
    :root {
      --bg: #f4f1ea;
      --card: #fffdfa;
      --text: #1d1b16;
      --accent: #0f766e;
      --accent-soft: #dff5f2;
      --border: #d8d2c6;
    }
    body {
      margin: 0;
      font-family: "Noto Sans TC", "Hiragino Sans", "Yu Gothic", sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top right, #e7f8ff 0%, var(--bg) 45%);
    }
    .wrap {
      max-width: 960px;
      margin: 28px auto;
      padding: 0 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.06);
      padding: 18px;
      margin-bottom: 16px;
    }
    h1 { margin: 0 0 8px; font-size: 22px; }
    p { margin: 0 0 12px; color: #4b5563; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
    input[type=text] {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      min-width: 220px;
      font-size: 14px;
      background: #fff;
    }
    button {
      border: none;
      border-radius: 10px;
      padding: 10px 14px;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      font-weight: 600;
    }
    button.secondary {
      background: #334155;
    }
    .quick button {
      background: var(--accent-soft);
      color: #0b534d;
      border: 1px solid #abd9d3;
    }
    pre {
      white-space: pre-wrap;
      background: #111827;
      color: #e5e7eb;
      border-radius: 12px;
      padding: 14px;
      min-height: 120px;
      margin: 0;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>NHK 一句一句閱讀</h1>
      <p>先一個一個看文法庫對照，再由你決定是否一句一句前進閱讀新聞。</p>
      <div class="row">
        <input id="userId" type="text" placeholder="User ID（可空白）" />
        <label><input id="furi" type="checkbox" /> 顯示平假名標註</label>
        <button id="loadLesson">開始閱讀</button>
      </div>
      <div class="row">
        <button id="prevSentence" class="secondary">上一句</button>
        <button id="nextSentence" class="secondary">下一句</button>
        <button id="detailExplain" class="secondary">詳細解釋</button>
      </div>
    </div>
    <div class="card">
      <pre id="output">尚未產生內容</pre>
    </div>
    <div class="card">
      <h1>整篇文章</h1>
      <p>獨立區塊，不會受上一句/下一句切換影響。</p>
      <pre id="articleOutput">尚未載入整篇文章</pre>
    </div>
  </div>
  <script>
    const output = document.getElementById("output");
    const articleOutput = document.getElementById("articleOutput");
    const userIdInput = document.getElementById("userId");
    const furiInput = document.getElementById("furi");

    function setOutput(text) {
      output.textContent = text;
    }

    function setArticleOutput(text) {
      articleOutput.textContent = text;
    }

    async function postJSON(url, body) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return await res.json();
    }

    document.getElementById("loadLesson").addEventListener("click", async () => {
      setOutput("生成中...");
      const data = await postJSON("/local-ui/lesson", {
        user_id: userIdInput.value || null,
        include_furigana: !!furiInput.checked
      });
      if (!data.ok) {
        setOutput("失敗: " + (data.error || "unknown"));
        return;
      }
      setOutput(data.message || "無內容");
      setArticleOutput(data.full_article || "尚未載入整篇文章");
    });

    document.getElementById("nextSentence").addEventListener("click", async () => {
      const data = await postJSON("/local-ui/next", {
        user_id: userIdInput.value || null
      });
      if (!data.ok) {
        setOutput("失敗: " + (data.error || "unknown"));
        return;
      }
      setOutput(data.message || "無內容");
    });

    document.getElementById("prevSentence").addEventListener("click", async () => {
      const data = await postJSON("/local-ui/prev", {
        user_id: userIdInput.value || null
      });
      if (!data.ok) {
        setOutput("失敗: " + (data.error || "unknown"));
        return;
      }
      setOutput(data.message || "無內容");
    });

    document.getElementById("detailExplain").addEventListener("click", async () => {
      setOutput("生成詳細解釋中...");
      const data = await postJSON("/local-ui/explain", {
        user_id: userIdInput.value || null
      });
      if (!data.ok) {
        setOutput("失敗: " + (data.error || "unknown"));
        return;
      }
      setOutput(data.message || "無內容");
    });

  </script>
</body>
</html>
"""


@app.get("/local-ui", response_class=HTMLResponse)
def local_ui() -> HTMLResponse:
    return HTMLResponse(LOCAL_UI_HTML)


@app.post("/local-ui/lesson")
def local_ui_lesson(req: LocalLessonRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    try:
        payload = build_local_reading_payload(include_furigana=req.include_furigana)
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    session = {
        **payload,
        "index": 0,
        "sentence_started": False,
        "grammar_index": 0,
    }
    refs = _grammar_refs(session)
    if not refs:
        session["sentence_started"] = True

    users = _load_local_ui_users()
    users[user_id] = session
    _save_local_ui_users(users)

    if refs:
        intro = _format_grammar_message(session)
        intro_message = intro + "\n\n按「詳細解釋」可查看目前這條文法的深入解析。進入句子模式後，會改為句子解釋。"
        mode = "grammar"
    else:
        intro = _format_sentence_message(session)
        intro_message = intro + "\n\n此篇未命中文法庫對照，已直接進入句子模式。"
        mode = "sentence"

    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "news_id": payload.get("news_id", ""),
            "message": _truncate(intro_message, 4900),
            "full_article": _format_full_article_message(session),
            "position": {"index": 0, "total": len(payload.get("sentences", []))},
            "mode": mode,
        }
    )


@app.post("/local-ui/answer")
def local_ui_answer(req: LocalAnswerRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    result = _handle_quiz_answer(user_id, req.answer)
    if not result:
        return JSONResponse({"ok": False, "error": "目前沒有待回答題目，或輸入內容無效。"}, status_code=400)
    return JSONResponse({"ok": True, "user_id": user_id, "result": result})


@app.post("/local-ui/next")
def local_ui_next(req: LocalStepRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    users = _load_local_ui_users()
    session = users.get(user_id)
    if not isinstance(session, dict):
        return JSONResponse({"ok": False, "error": "請先按「開始閱讀」。"}, status_code=400)

    sentences_raw = session.get("sentences", [])
    sentences = sentences_raw if isinstance(sentences_raw, list) else []
    if not sentences:
        return JSONResponse({"ok": False, "error": "目前沒有可顯示的句子。"}, status_code=400)

    sentence_started = bool(session.get("sentence_started", False))
    refs = _grammar_refs(session)

    if not sentence_started:
        if refs:
            grammar_index = _current_grammar_index(session, refs)
            if grammar_index < len(refs) - 1:
                session["grammar_index"] = grammar_index + 1
                users[user_id] = session
                _save_local_ui_users(users)
                return JSONResponse(
                    {
                        "ok": True,
                        "user_id": user_id,
                        "message": _format_grammar_message(session),
                        "position": {"index": 0, "total": len(sentences)},
                        "mode": "grammar",
                    }
                )

        session["sentence_started"] = True
        session["index"] = 0
        users[user_id] = session
        _save_local_ui_users(users)
        done = len(sentences) <= 1
        suffix = "\n\n（文法已看完，進入新聞句子模式）"
        if done:
            suffix += "\n（已到最後一句）"
        return JSONResponse(
            {
                "ok": True,
                "user_id": user_id,
                "message": _format_sentence_message(session) + suffix,
                "position": {"index": 1, "total": len(sentences)},
                "mode": "sentence",
            }
        )

    current = int(session.get("index", 0))
    next_index = min(current + 1, len(sentences) - 1)
    session["index"] = next_index
    users[user_id] = session
    _save_local_ui_users(users)

    done = next_index >= len(sentences) - 1
    suffix = "\n\n（已到最後一句）" if done else ""
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "message": _format_sentence_message(session) + suffix,
            "position": {"index": next_index + 1, "total": len(sentences)},
            "mode": "sentence",
        }
    )


@app.post("/local-ui/prev")
def local_ui_prev(req: LocalStepRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    users = _load_local_ui_users()
    session = users.get(user_id)
    if not isinstance(session, dict):
        return JSONResponse({"ok": False, "error": "請先按「開始閱讀」。"}, status_code=400)

    sentences_raw = session.get("sentences", [])
    sentences = sentences_raw if isinstance(sentences_raw, list) else []
    if not sentences:
        return JSONResponse({"ok": False, "error": "目前沒有可顯示的句子。"}, status_code=400)
    sentence_started = bool(session.get("sentence_started", False))
    if not sentence_started:
        refs = _grammar_refs(session)
        if not refs:
            return JSONResponse({"ok": False, "error": "目前沒有可切換的文法。"}, status_code=400)
        current_g = _current_grammar_index(session, refs)
        prev_g = max(current_g - 1, 0)
        session["grammar_index"] = prev_g
        users[user_id] = session
        _save_local_ui_users(users)
        prefix = "（已是第一個文法）\n\n" if prev_g == 0 and current_g == 0 else ""
        return JSONResponse(
            {
                "ok": True,
                "user_id": user_id,
                "message": prefix + _format_grammar_message(session),
                "position": {"index": 0, "total": len(sentences)},
                "mode": "grammar",
            }
        )

    current = int(session.get("index", 0))
    prev_index = max(current - 1, 0)
    session["index"] = prev_index
    users[user_id] = session
    _save_local_ui_users(users)

    prefix = "（已是第一句）\n\n" if prev_index == 0 and current == 0 else ""
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "message": prefix + _format_sentence_message(session),
            "position": {"index": prev_index + 1, "total": len(sentences)},
            "mode": "sentence",
        }
    )


@app.post("/local-ui/grammar")
def local_ui_grammar(req: LocalStepRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    users = _load_local_ui_users()
    session = users.get(user_id)
    if not isinstance(session, dict):
        return JSONResponse({"ok": False, "error": "請先按「開始閱讀」。"}, status_code=400)
    refs = _grammar_refs(session)
    if not refs:
        return JSONResponse(
            {
                "ok": True,
                "user_id": user_id,
                "message": _format_sentence_message(session),
                "mode": "sentence",
            }
        )
    return JSONResponse({"ok": True, "user_id": user_id, "message": _format_grammar_message(session), "mode": "grammar"})


@app.post("/local-ui/explain")
def local_ui_explain(req: LocalStepRequest) -> JSONResponse:
    user_id = _resolve_user_id(req.user_id)
    users = _load_local_ui_users()
    session = users.get(user_id)
    if not isinstance(session, dict):
        return JSONResponse({"ok": False, "error": "請先按「開始閱讀」。"}, status_code=400)

    sentence_started = bool(session.get("sentence_started", False))
    if sentence_started:
        return JSONResponse(
            {
                "ok": True,
                "user_id": user_id,
                "message": _format_sentence_explanation_message(session),
                "warning": "",
                "cached": False,
                "mode": "sentence",
            }
        )

    refs = _grammar_refs(session)
    if not refs:
        return JSONResponse({"ok": False, "error": "目前沒有可解釋的文法庫對照。"}, status_code=400)
    current_idx = _current_grammar_index(session, refs)
    current_ref = refs[current_idx]

    refs_for_cache = _normalize_refs_for_cache([current_ref])
    cache_key = _build_explain_cache_key(refs_for_cache)
    cache_items = _load_explain_cache_items()
    cached = cache_items.get(cache_key)
    if isinstance(cached, dict):
        cached_details_raw = cached.get("details", [])
        cached_details = cached_details_raw if isinstance(cached_details_raw, list) else []
        if cached_details:
            message = _format_detailed_explanation_message(
                session,
                [item for item in cached_details if isinstance(item, dict)],
                warning="使用快取結果（未重複產生離線詳細解釋）。",
            )
            return JSONResponse(
                {
                    "ok": True,
                    "user_id": user_id,
                    "message": message,
                    "warning": "",
                    "cached": True,
                    "mode": "grammar",
                }
            )

    warning = ""
    details = _find_offline_detail_for_ref(session, current_ref)
    if not details:
        generated = build_detailed_explanations_offline(
            article_title=str(session.get("title", "")).strip(),
            article_paragraphs=[str(line).strip() for line in session.get("sentences", []) if str(line).strip()],
            grammar_references=[{str(k): str(v) for k, v in current_ref.items()}],
        )
        details = generated[:1]
        warning = "本篇尚無預先生成資料，已改用本地離線模板產生詳細解釋。"
    if details:
        _save_explain_cache_entry(cache_key, refs_for_cache, details)

    message = _format_detailed_explanation_message(session, details, warning=warning)
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "message": message,
            "warning": warning,
            "cached": False,
            "mode": "grammar",
        }
    )


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(default="")) -> JSONResponse:
    if not line_client.is_configured:
        raise HTTPException(status_code=500, detail="LINE credentials are not configured.")

    body = await request.body()
    if not line_client.verify_signature(body, x_line_signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")

    payload = await request.json()
    events = payload.get("events", [])

    for event in events:
        source = event.get("source", {})
        user_id = source.get("userId")
        if not user_id and settings.local_test_mode:
            user_id = settings.local_test_user_id
        event_type = event.get("type")
        reply_token = event.get("replyToken")
        if not reply_token and settings.local_test_mode:
            reply_token = "local-reply-token"
        text = (
            event.get("message", {}).get("text", "").strip()
            if event_type == "message"
            else ""
        )

        if user_id:
            newly_added = _add_subscriber(user_id)
            if newly_added and reply_token and not _is_lesson_trigger(text):
                line_client.reply_text(
                    reply_token,
                    "已註冊每日文法推播。你可以輸入「今日文法」立即收到一篇。",
                )
                continue

        if user_id and _is_lesson_trigger(text) and reply_token:
            try:
                include_furigana = _wants_furigana(text)
                _news_id, message = build_line_digest_for_user(user_id, include_furigana=include_furigana)
                line_client.reply_text(reply_token, message)
            except Exception:  # pragma: no cover
                line_client.reply_text(reply_token, "執行失敗，請稍後再試。")
            continue

        if user_id and reply_token:
            answer_result = _handle_quiz_answer(user_id, text)
            if answer_result:
                line_client.reply_text(reply_token, answer_result)

    return JSONResponse({"ok": True})
