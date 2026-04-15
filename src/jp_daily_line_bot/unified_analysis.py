from __future__ import annotations

import re
from typing import Any


def build_unified_analysis(
    *,
    news_id: str,
    published_at: str | None,
    url: str,
    article_sentences: list[str],
    grammar_candidates: list[dict[str, str]],
) -> dict[str, Any]:
    clean_sentences = [str(s).strip() for s in article_sentences if str(s).strip()]
    grammar_points = _build_grammar_points(clean_sentences, grammar_candidates)
    sentence_analysis = _build_sentence_analysis(clean_sentences, grammar_points)
    exercises = _build_exercises(grammar_points)

    return {
        "meta": {
            "news_id": news_id.strip(),
            "published_at": (published_at or "").strip(),
            "url": url.strip(),
        },
        "article_sentences": clean_sentences,
        "grammar_points": grammar_points,
        "sentence_analysis": sentence_analysis,
        "exercises": exercises,
    }


def _normalize_pattern_surface(text: str) -> str:
    return re.sub(r"[〜～\s／/・、，。,.！!？?()（）【】\[\]{}「」]", "", text)


def _strip_tilde_pattern(text: str) -> str:
    return re.sub(r"^[〜～\-\s]+", "", text).strip()


def _find_text_snippet(sentences: list[str], pattern: str) -> str:
    plain = _strip_tilde_pattern(pattern)
    surface = _normalize_pattern_surface(plain)
    if not surface:
        return ""
    for line in sentences:
        if surface in _normalize_pattern_surface(line):
            return line
    return ""


def _common_confusion(pattern: str) -> str:
    normalized = _normalize_pattern_surface(pattern)
    if "てある" in normalized:
        return "常與「〜ている」混淆；「〜てある」偏人為結果狀態。"
    if "という" in normalized:
        return "常與動詞「と言う（說）」混淆；此處多為連體修飾。"
    if "です" in normalized:
        return "「です／でした」是禮貌判斷句尾，不等於存在動詞。"
    if "ので" in normalized:
        return "常與「〜から」混淆；「ので」語氣通常較客觀、柔和。"
    if "ことができる" in normalized:
        return "常與「〜られる」混淆；後者還可能表示被動或尊敬。"
    return "可和近義文法做對照，先看接續詞性，再看語氣差異。"


def _extra_example(pattern: str) -> tuple[str, str]:
    normalized = _normalize_pattern_surface(pattern)
    if "てある" in normalized:
        return ("机の上に資料が並べてあります。", "桌上已經把資料排好了。")
    if "という" in normalized:
        return ("田中さんという人が受付にいます。", "有位叫田中的人正在櫃檯。")
    if "です" in normalized:
        return ("この部屋は静かでした。", "這個房間之前很安靜。")
    if "ので" in normalized:
        return ("雨が強いので、早く帰ります。", "因為雨很大，所以我提早回去。")
    return ("今日は図書館で勉強します。", "我今天在圖書館讀書。")


def _build_grammar_points(
    sentences: list[str],
    grammar_candidates: list[dict[str, str]],
    max_points: int = 3,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw in grammar_candidates:
        if not isinstance(raw, dict):
            continue
        pattern = str(raw.get("pattern", "")).strip() or str(raw.get("title", "")).strip()
        if not pattern:
            continue
        key = _normalize_pattern_surface(pattern)
        if not key or key in seen:
            continue

        snippet = _find_text_snippet(sentences, pattern)
        # Keep only points that can be tied back to the article.
        if not snippet:
            continue

        meaning = str(raw.get("meaning", "")).strip()
        explanation = str(raw.get("explanation", "")).strip()
        reference_example = str(raw.get("example", "")).strip()
        reference_example_kanji = str(raw.get("example_kanji", "")).strip()
        if explanation:
            usage = explanation
        elif meaning:
            usage = f"此文法核心意思是「{meaning}」。"
        else:
            usage = "此文法用於補充句子語氣與結構。"

        example_ja, example_zh = _extra_example(pattern)
        out.append(
            {
                "pattern": pattern,
                "level": str(raw.get("level", "")).strip(),
                "source_url": str(raw.get("url", "")).strip(),
                "meaning": meaning,
                "from_text": snippet or str(raw.get("example", "")).strip(),
                "usage_zh": usage,
                "confusion_zh": _common_confusion(pattern),
                "reference_example_ja": reference_example,
                "reference_example_kanji": reference_example_kanji,
                "extra_example_ja": example_ja,
                "extra_example_zh": example_zh,
                "detailed_explanation_zh": usage,
                "example_breakdown_zh": f"文章中的例句是「{snippet}」，此文法在此處影響句子語氣或結構。",
                "common_confusion_zh": _common_confusion(pattern),
            }
        )
        seen.add(key)
        if len(out) >= max(1, max_points):
            break

    return out


def _suggest_natural_sentence(sentence: str) -> str:
    text = sentence.strip()
    # Common NHK shorthand: "13日人..." -> "13日に人..."
    text = re.sub(r"([0-9０-９]+日)(人|男性|女性|子ども|子供)", r"\1に\2", text)
    return text


def _simple_translation(sentence: str) -> str:
    line = sentence.strip()
    if "亡くなっているのが見つかりました" in line:
        return "在該地發現有人已死亡。"
    if "とわかりました" in line:
        return "經調查後確認了身分。"
    if "いなくなっていました" in line:
        return "之後處於失蹤狀態。"
    if "場所は" in line and "でした" in line:
        return "這句在說明發現地點。"
    if "という数字が書いてある" in line:
        return "這句在描述衣服上的數字標記。"
    if "はいていませんでした" in line:
        return "這句在描述當時未穿鞋。"
    if "のではないか" in line and "調べています" in line:
        return "警方正在以可能涉案方向持續調查。"
    return "請依上下文理解此句的事件描述。"


def _sentence_points(sentence: str, suggested: str) -> list[str]:
    points: list[str] = []
    merged = f"{sentence} {suggested}"
    if "で、" in merged or merged.endswith("で"):
        points.append("「で」可標示事件發生地點。")
    if re.search(r"[0-9０-９]+日(に|、)", merged):
        points.append("日期可用「〜日に」或新聞式「〜日、」標示時間。")
    if "ている" in merged:
        points.append("「〜ている」在新聞裡常表持續狀態或結果狀態。")
    if "という" in merged:
        points.append("「〜という」常用於命名或補充說明名詞。")
    if "のが見つか" in merged:
        points.append("「普通形＋のが見つかる」表示某件事被發現。")
    if "のではないか" in merged:
        points.append("「〜のではないか」表示推測。")
    if "たあと" in merged:
        points.append("「〜たあと」表示在某動作之後。")
    if not points:
        points.append("先抓句尾與主要動詞，再判斷時態與語氣。")
    return points[:4]


def _build_sentence_analysis(
    sentences: list[str],
    grammar_points: list[dict[str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, sentence in enumerate(sentences, start=1):
        suggested = _suggest_natural_sentence(sentence)
        if suggested == sentence:
            suggested = ""
        normalized_sentence = _normalize_pattern_surface(sentence)
        matched_patterns: list[str] = []
        for gp in grammar_points:
            pattern = str(gp.get("pattern", "")).strip()
            surface = _normalize_pattern_surface(_strip_tilde_pattern(pattern))
            if not surface:
                continue
            if surface in normalized_sentence:
                matched_patterns.append(pattern)

        out.append(
            {
                "index": idx,
                "original": sentence,
                "suggested_natural_ja": suggested,
                "key_points": _sentence_points(sentence, suggested),
                "translation_zh": _simple_translation(sentence),
                "matched_grammar_patterns": matched_patterns[:3],
            }
        )
    return out


def _build_exercises(grammar_points: list[dict[str, str]]) -> dict[str, Any]:
    first_pattern = ""
    if grammar_points:
        first_pattern = str(grammar_points[0].get("pattern", "")).strip()

    short_answer_prompt = "「さくら」＿＿＿名前のカフェに行きました。"
    short_answer = "という" if "という" in first_pattern or not first_pattern else _strip_tilde_pattern(first_pattern)

    return {
        "mcq": {
            "question_zh": "哪一句最自然地表達「牆上貼著地圖（人為貼好後的狀態）」？",
            "options": {
                "A": "壁に地図が貼っている。",
                "B": "壁に地図が貼ってある。",
                "C": "壁に地図という貼る。",
                "D": "壁に地図が貼るです。",
            },
            "answer": "B",
            "explanation_zh": "「〜てある」表示人為動作完成後保留的狀態。",
        },
        "short_answer": {
            "question_zh": "請填入最適當的文法。",
            "prompt_ja": short_answer_prompt,
            "reference_answer": short_answer,
            "explanation_zh": "「名詞 + という + 名詞」可表示「叫做～的…」。",
        },
    }
