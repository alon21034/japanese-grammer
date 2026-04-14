from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .zh_tw import to_zh_tw

@dataclass(frozen=True)
class NHKEasyArticle:
    news_id: str
    title: str
    url: str
    published_at: str | None
    paragraphs_plain: list[str]
    paragraphs_with_furigana: list[str]
    grammar_references: list[dict[str, str]] = field(default_factory=list)
    offline_detailed_explanations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LessonQuestion:
    qtype: str
    question_zh: str
    question_ja: str
    options: dict[str, str]
    accepted_answers: list[str]
    explanation_zh: str


@dataclass(frozen=True)
class GeneratedLesson:
    grammar_points: list[dict[str, str]]
    questions: list[LessonQuestion]


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    if not isinstance(data, dict):
        return dict(default)
    return data


def load_nhk_index(data_dir: Path) -> list[dict[str, Any]]:
    index_path = data_dir / "nhk_easy" / "index.json"
    payload = _load_json(index_path, default={"items": []})
    items = payload.get("items", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def choose_next_news(index_items: list[dict[str, Any]], sent_news_ids: list[str]) -> str:
    seen = {str(news_id) for news_id in sent_news_ids if news_id}
    for item in index_items:
        news_id = str(item.get("news_id", "")).strip()
        if news_id and news_id not in seen:
            return news_id
    for item in index_items:
        news_id = str(item.get("news_id", "")).strip()
        if news_id:
            return news_id
    raise RuntimeError("No NHK Easy news_id found in index.")


def load_nhk_article(data_dir: Path, news_id: str) -> NHKEasyArticle:
    path = data_dir / "nhk_easy" / "articles" / f"{news_id}.json"
    payload = _load_json(path, default={})
    if not payload:
        raise RuntimeError(
            f"NHK article file not found for {news_id}. Run scripts/sync_nhk_easy.py first."
        )

    paragraphs_plain = payload.get("paragraphs_plain") or []
    if not isinstance(paragraphs_plain, list):
        paragraphs_plain = []
    paragraphs_furi = payload.get("paragraphs_with_furigana") or []
    if not isinstance(paragraphs_furi, list):
        paragraphs_furi = []

    clean_plain = [str(line).strip() for line in paragraphs_plain if str(line).strip()]
    clean_furi = [str(line).strip() for line in paragraphs_furi if str(line).strip()]
    grammar_refs_raw = payload.get("grammar_references") or []
    grammar_refs = grammar_refs_raw if isinstance(grammar_refs_raw, list) else []
    grammar_refs_clean = [item for item in grammar_refs if isinstance(item, dict)]
    offline_details_raw = payload.get("offline_detailed_explanations") or []
    offline_details = offline_details_raw if isinstance(offline_details_raw, list) else []
    offline_details_clean = [item for item in offline_details if isinstance(item, dict)]
    if not clean_plain:
        raise RuntimeError(f"NHK article {news_id} has no usable paragraphs.")

    return NHKEasyArticle(
        news_id=news_id,
        title=str(payload.get("title", "")).strip() or news_id,
        url=str(payload.get("url", "")).strip() or "",
        published_at=str(payload.get("published_at", "")).strip() or None,
        paragraphs_plain=clean_plain,
        paragraphs_with_furigana=clean_furi,
        grammar_references=grammar_refs_clean,
        offline_detailed_explanations=offline_details_clean,
    )


def build_article_context(article: NHKEasyArticle, max_paragraphs: int = 8) -> str:
    lines = article.paragraphs_plain[:max(max_paragraphs, 1)]
    return "\n".join(lines)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _split_lines_to_messages(lines: list[str], max_len: int = 4900) -> list[str]:
    messages: list[str] = []
    current: list[str] = []

    for line in lines:
        candidate = "\n".join(current + [line]).strip()
        if len(candidate) <= max_len:
            current.append(line)
            continue

        if current:
            messages.append("\n".join(current).strip())
            current = []

        if len(line) <= max_len:
            current.append(line)
        else:
            messages.append(_truncate(line, max_len))

    if current:
        messages.append("\n".join(current).strip())

    return [msg for msg in messages if msg]


def _normalize_options(raw: dict[str, Any]) -> dict[str, str]:
    options: dict[str, str] = {}
    for key in ("A", "B", "C", "D"):
        options[key] = str(raw.get(key, "")).strip()
    return options


def _extract_furigana_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Match compact "term(reading)" chunks and keep only kanji/digit-based terms.
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


def _collect_furigana_pairs(lines: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in lines:
        for pair in _extract_furigana_pairs(line):
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _build_furigana_glossary(lines: list[str]) -> dict[str, str]:
    return {k: r for k, r in _collect_furigana_pairs(lines)}


def _extract_kanji_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[0-9０-９一-龯々ヶ]+", text):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _short_answer_kanji_hints(
    question: dict[str, Any],
    furigana_glossary: dict[str, str],
    max_items: int = 10,
) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()

    accepted_raw = question.get("accepted_answers", [])
    accepted_answers = accepted_raw if isinstance(accepted_raw, list) else []
    for ans in accepted_answers:
        ans_text = str(ans).strip()
        if not ans_text:
            continue
        for token in _extract_kanji_tokens(ans_text):
            if token in seen:
                continue
            reading = furigana_glossary.get(token)
            if not reading:
                continue
            seen.add(token)
            hints.append(f"{token}({reading})")
            if len(hints) >= max_items:
                return hints
    return hints


def build_fallback_lesson(article: NHKEasyArticle) -> GeneratedLesson:
    first_sentence = article.paragraphs_plain[0]
    grammar_points = [
        {
            "pattern": "〜について",
            "meaning_zh": "關於、針對",
            "explanation_zh": "用來引入話題，說明後文要談的對象。",
            "example_from_article": first_sentence,
        },
        {
            "pattern": "〜ている",
            "meaning_zh": "持續狀態／正在進行",
            "explanation_zh": "在新聞文體常用來描述現在狀態或進行中的動作。",
            "example_from_article": first_sentence,
        },
    ]
    questions = [
        LessonQuestion(
            qtype="mcq",
            question_zh="「〜について」最接近哪個意思？",
            question_ja="",
            options={
                "A": "一定要",
                "B": "關於、針對",
                "C": "雖然、但是",
                "D": "如果、要是",
            },
            accepted_answers=["B"],
            explanation_zh="「〜について」表示討論主題，意思是「關於……」。",
        ),
        LessonQuestion(
            qtype="short_answer",
            question_zh="把「警察正在調查。」翻成自然日文短句。",
            question_ja="例：_____",
            options={"A": "", "B": "", "C": "", "D": ""},
            accepted_answers=["警察は調べています", "警察が調べています"],
            explanation_zh="可用「〜ている」表進行／持續狀態。",
        ),
    ]
    return GeneratedLesson(grammar_points=grammar_points, questions=questions)


def lesson_from_payload(payload: dict[str, Any]) -> GeneratedLesson:
    raw_points = payload.get("grammar_points", [])
    if not isinstance(raw_points, list):
        raw_points = []

    grammar_points: list[dict[str, str]] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, dict):
            continue
        point = {
            "pattern": str(raw_point.get("pattern", "")).strip(),
            "meaning_zh": str(raw_point.get("meaning_zh", "")).strip(),
            "explanation_zh": str(raw_point.get("explanation_zh", "")).strip(),
            "example_from_article": str(raw_point.get("example_from_article", "")).strip(),
        }
        if point["pattern"] and point["meaning_zh"] and point["explanation_zh"]:
            grammar_points.append(point)

    raw_questions = payload.get("questions", [])
    if not isinstance(raw_questions, list):
        raw_questions = []

    questions: list[LessonQuestion] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        qtype = str(item.get("type", "")).strip().lower()
        if qtype not in {"mcq", "short_answer"}:
            continue

        options_raw = item.get("options", {})
        options = _normalize_options(options_raw if isinstance(options_raw, dict) else {})

        raw_answers = item.get("accepted_answers", [])
        accepted_answers = [str(ans).strip() for ans in raw_answers if str(ans).strip()] if isinstance(raw_answers, list) else []

        question = LessonQuestion(
            qtype=qtype,
            question_zh=str(item.get("question_zh", "")).strip(),
            question_ja=str(item.get("question_ja", "")).strip(),
            options=options,
            accepted_answers=accepted_answers,
            explanation_zh=str(item.get("explanation_zh", "")).strip(),
        )

        if not question.question_zh or not question.explanation_zh or not question.accepted_answers:
            continue
        if question.qtype == "mcq" and not any(question.options.values()):
            continue

        questions.append(question)

    if not grammar_points:
        raise RuntimeError("Lesson payload has no usable grammar points.")
    if not questions:
        raise RuntimeError("Lesson payload has no usable questions.")

    return GeneratedLesson(grammar_points=grammar_points[:3], questions=questions[:3])


def format_question_message(
    question: dict[str, Any],
    idx: int,
    total: int,
    *,
    furigana_glossary: dict[str, str] | None = None,
) -> str:
    qtype = str(question.get("type", "")).strip().lower()
    question_zh = to_zh_tw(str(question.get("question_zh", "")).strip())
    question_ja = str(question.get("question_ja", "")).strip()
    explanation = to_zh_tw(str(question.get("explanation_zh", "")).strip())

    lines = [f"【練習題 {idx}/{total}】", _truncate(question_zh, 220)]
    if question_ja:
        lines.append(_truncate(f"提示日文：{question_ja}", 220))

    if qtype == "mcq":
        options_raw = question.get("options", {})
        options = options_raw if isinstance(options_raw, dict) else {}
        for k in ("A", "B", "C", "D"):
            value = str(options.get(k, "")).strip()
            if value:
                lines.append(f"{k}. {_truncate(value, 120)}")
        lines.append("請回覆 A/B/C/D（或 1/2/3/4）。")
    else:
        lines.append("請直接輸入簡短日文答案。")
        hints = _short_answer_kanji_hints(question, furigana_glossary or {})
        if hints:
            lines.append(_truncate(f"可用漢字提示：{'、'.join(hints)}", 260))

    if explanation:
        lines.append(_truncate(f"作答方向：{explanation}", 160))

    return _truncate("\n".join(lines), 4900)


def format_lesson_messages(
    article: NHKEasyArticle,
    lesson: GeneratedLesson,
    include_furigana: bool,
    grammar_references: list[dict[str, str]] | None = None,
) -> list[str]:
    furigana_glossary = _build_furigana_glossary(article.paragraphs_with_furigana)
    article_header = ["【NHK 日文新聞全文】", article.title]
    if article.published_at:
        article_header.append(f"時間：{article.published_at}")
    article_header.append("顯示模式：原文")
    article_header.append("")
    article_header.extend(article.paragraphs_plain)
    if article.url:
        article_header.extend(["", f"原文：{article.url}"])

    article_messages = _split_lines_to_messages(article_header, max_len=4900)

    if include_furigana and article.paragraphs_with_furigana:
        furigana_lines = ["【平假名標註（對照）】"]
        for k, r in _collect_furigana_pairs(article.paragraphs_with_furigana):
            furigana_lines.append(f"{k}={r}")
        if len(furigana_lines) > 1:
            article_messages.extend(_split_lines_to_messages(furigana_lines, max_len=4900))

    grammar_lines = ["【重點文法】"]
    for idx, point in enumerate(lesson.grammar_points[:3], start=1):
        pattern = _truncate(str(point.get("pattern", "")).strip(), 50)
        meaning = _truncate(to_zh_tw(str(point.get("meaning_zh", "")).strip()), 80)
        explain = _truncate(to_zh_tw(str(point.get("explanation_zh", "")).strip()), 180)
        example = _truncate(str(point.get("example_from_article", "")).strip(), 120)
        grammar_lines.append(f"{idx}. {pattern}：{meaning}")
        grammar_lines.append(f"- 說明：{explain}")
        if example:
            grammar_lines.append(f"- 例句：{example}")

    if grammar_references:
        grammar_lines.append("")
        grammar_lines.append("【文法庫對照】")
        for idx, ref in enumerate(grammar_references[:3], start=1):
            pattern = _truncate(str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip(), 60)
            level = str(ref.get("level", "")).strip()
            meaning = _truncate(to_zh_tw(str(ref.get("meaning", "")).strip()), 90)
            source = _truncate(str(ref.get("url", "")).strip(), 130)
            matched_terms = _truncate(str(ref.get("matched_terms", "")).strip(), 60)
            head = f"{idx}. {pattern}" + (f" [{level}]" if level else "")
            grammar_lines.append(head)
            if meaning:
                grammar_lines.append(f"- 意味：{meaning}")
            if matched_terms:
                grammar_lines.append(f"- 關聯詞：{matched_terms}")
            if source:
                grammar_lines.append(f"- 來源：{source}")

    first_question = {
        "type": lesson.questions[0].qtype,
        "question_zh": lesson.questions[0].question_zh,
        "question_ja": lesson.questions[0].question_ja,
        "options": lesson.questions[0].options,
        "accepted_answers": lesson.questions[0].accepted_answers,
        "explanation_zh": lesson.questions[0].explanation_zh,
    }

    messages = article_messages + [
        _truncate("\n".join(grammar_lines), 4900),
        format_question_message(
            first_question,
            1,
            len(lesson.questions),
            furigana_glossary=furigana_glossary,
        ),
    ]

    # LINE reply/push supports at most 5 messages in one call.
    if len(messages) <= 5:
        return messages
    keep_tail = messages[-2:]
    head_budget = 3
    head = messages[:head_budget]
    return head + keep_tail


def format_push_digest_message(
    article: NHKEasyArticle,
    lesson: GeneratedLesson,
    *,
    include_furigana: bool,
    grammar_references: list[dict[str, str]] | None = None,
) -> str:
    sentence_lines = (
        article.paragraphs_with_furigana
        if include_furigana and article.paragraphs_with_furigana
        else article.paragraphs_plain
    )
    news_sentence = sentence_lines[0] if sentence_lines else ""

    lines: list[str] = ["【今日一句】", _truncate(news_sentence, 240)]
    if article.url:
        lines.append(f"原文：{_truncate(article.url, 180)}")

    grammar_lines: list[str] = []
    for point in lesson.grammar_points[:2]:
        pattern = _truncate(str(point.get("pattern", "")).strip(), 30)
        meaning = _truncate(to_zh_tw(str(point.get("meaning_zh", "")).strip()), 70)
        explanation = _truncate(to_zh_tw(str(point.get("explanation_zh", "")).strip()), 90)
        if not (pattern and meaning):
            continue
        grammar_lines.append(f"- {pattern}：{meaning}")
        if explanation:
            grammar_lines.append(f"  {_truncate(explanation, 90)}")

    if not grammar_lines and grammar_references:
        for ref in grammar_references[:2]:
            pattern = _truncate(str(ref.get("pattern", "")).strip(), 30)
            meaning = _truncate(to_zh_tw(str(ref.get("meaning", "")).strip()), 70)
            if pattern:
                grammar_lines.append(f"- {pattern}" + (f"：{meaning}" if meaning else ""))

    if grammar_lines:
        lines.append("")
        lines.append("【搭配文法】")
        lines.extend(grammar_lines)

    example_lines: list[str] = []
    if grammar_references:
        for ref in grammar_references[:2]:
            pattern = _truncate(str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip(), 30)
            example = _truncate(str(ref.get("example", "")).strip(), 90)
            example_kanji = _truncate(str(ref.get("example_kanji", "")).strip(), 90)
            if pattern and example:
                example_lines.append(f"- {pattern}：{example}")
                if example_kanji:
                    example_lines.append(f"  漢字標註：{example_kanji}")

    if example_lines:
        lines.append("")
        lines.append("【文法庫例句】")
        lines.extend(example_lines)

    return _truncate("\n".join(lines), 4900)


def pending_quiz_payload(
    article: NHKEasyArticle,
    lesson: GeneratedLesson,
    *,
    include_furigana: bool,
) -> dict[str, Any]:
    furigana_glossary = _build_furigana_glossary(article.paragraphs_with_furigana)
    questions = [
        {
            "type": q.qtype,
            "question_zh": q.question_zh,
            "question_ja": q.question_ja,
            "options": q.options,
            "accepted_answers": q.accepted_answers,
            "explanation_zh": q.explanation_zh,
        }
        for q in lesson.questions[:3]
    ]
    return {
        "news_id": article.news_id,
        "title": article.title,
        "url": article.url,
        "include_furigana": include_furigana,
        "furigana_glossary": furigana_glossary,
        "questions": questions,
        "current_index": 0,
    }


def normalize_short_answer(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    normalized = normalized.replace(" ", "")
    normalized = re.sub(r"[。．、,！!？?「」『』（）()\[\]{}]", "", normalized)
    return normalized


def parse_user_answer(text: str) -> str | None:
    normalized = text.strip().upper()
    if not normalized:
        return None

    mapping = {
        "1": "A",
        "2": "B",
        "3": "C",
        "4": "D",
        "１": "A",
        "２": "B",
        "３": "C",
        "４": "D",
        "Ａ": "A",
        "Ｂ": "B",
        "Ｃ": "C",
        "Ｄ": "D",
    }
    if normalized in {"A", "B", "C", "D"}:
        return normalized
    if normalized in mapping:
        return mapping[normalized]

    match = re.fullmatch(r"([ABCD])(?:[\s\.\)]*)", normalized)
    if match:
        return match.group(1)
    return None
