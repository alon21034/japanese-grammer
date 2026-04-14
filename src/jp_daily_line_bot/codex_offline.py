from __future__ import annotations

import re
from typing import Any


def build_detailed_explanations_offline(
    *,
    article_title: str,
    article_paragraphs: list[str],
    grammar_references: list[dict[str, str]],
) -> list[dict[str, str]]:
    _ = article_title
    article_text = "\n".join(article_paragraphs)
    details: list[dict[str, str]] = []
    for ref_raw in grammar_references[:3]:
        ref = ref_raw if isinstance(ref_raw, dict) else {}
        pattern = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        if not pattern:
            continue
        meaning = str(ref.get("meaning", "")).strip()
        explanation = str(ref.get("explanation", "")).strip()
        example = str(ref.get("example", "")).strip()

        detailed = _build_detailed_explanation(pattern, meaning, explanation)
        breakdown = _build_example_breakdown(pattern, example, article_text)
        confusion = _build_common_confusion(pattern)
        details.append(
            {
                "pattern": pattern,
                "detailed_explanation_zh": detailed,
                "example_breakdown_zh": breakdown,
                "common_confusion_zh": confusion,
                "source": "codex_offline_v1",
            }
        )
    return details


def _build_detailed_explanation(pattern: str, meaning: str, explanation: str) -> str:
    lines: list[str] = []
    if meaning:
        lines.append(f"此文法的核心意思是「{meaning}」。")
    else:
        lines.append("此文法用來表達特定語氣與句子功能。")

    if explanation:
        lines.append(explanation)
    else:
        lines.append("在句子中通常接在符合語法接續的位置，作用是補充語氣、原因或說話者意圖。")

    lines.append("閱讀時可先抓住句尾或文法片段，再回看前項詞性與語境，通常較容易判斷用法。")
    return " ".join(lines).strip()


def _build_example_breakdown(pattern: str, example: str, article_text: str) -> str:
    if not example:
        return "目前沒有可用例句，建議先從文章原句找出相同文法片段，再對照其前後語境。"

    sentence_part = f"例句「{example}」"
    if pattern and pattern in example:
        sentence_part += f"中可直接看到「{pattern}」"
    else:
        sentence_part += "展示了此文法的實際句型"

    article_hint = ""
    if pattern and pattern in article_text:
        article_hint = "，而且這個文法也出現在本篇新聞中，可對照相同語感。"
    else:
        article_hint = "，可先理解句子功能後，再回到新聞中找相近語意。"
    return sentence_part + article_hint


def _build_common_confusion(pattern: str) -> str:
    normalized = re.sub(r"\s+", "", pattern)
    if "ように" in normalized:
        return "常和「〜ために」混淆；前者可用於非意志性結果或能力變化，後者多用於明確目的。"
    if "ことができる" in normalized:
        return "常和「〜られる」混淆；前者偏一般能力/可能，後者還可能表示被動或尊敬。"
    if "ので" in normalized:
        return "常和「〜から」混淆；「ので」語氣較柔和、客觀，「から」主觀斷定較強。"
    if "たり" in normalized:
        return "常和「〜て」單純並列混淆；「〜たり〜たりする」通常表示列舉代表性動作。"
    return "易混淆點通常在於語氣強弱與可接續詞性，建議和近義文法做最小對比記憶。"


_GENERAL_PATTERN_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("ことができる", "能夠、可以", "表示能力或客觀上可行的可能性。"),
    ("ようにする", "盡量做到…", "表示說話者有意識地讓狀態往某方向維持或改變。"),
    ("ように", "為了、以便", "常用於表示目的、變化結果，或前後句的關聯方式。"),
    ("ている", "正在…／持續狀態", "表示動作進行中，或某狀態持續存在。"),
    ("ので", "因為…", "表示理由，語氣通常比「から」更柔和、客觀。"),
    ("から", "因為…／所以…", "可表示原因或起點；做原因時語氣通常較直接。"),
    ("たり", "…啦…啦（列舉）", "用於列舉代表性動作，暗示不只一個行為。"),
    ("ない", "不…", "是否定表現，常用於描述沒有做某事或某狀態不存在。"),
    ("たい", "想要…", "表示說話者的意願或希望去做某動作。"),
)


def build_sentence_explanation_offline(
    *,
    sentence: str,
    max_points: int = 2,
) -> dict[str, Any]:
    text = sentence.strip()
    if not text:
        return {
            "summary_zh": "目前沒有可解釋的句子內容。",
            "chunks": [],
            "grammar_points": [],
            "steps": [],
            "source": "codex_offline_sentence_v1",
        }

    compact = re.sub(r"[ 　]", "", text)
    chunks = [part.strip() for part in re.split(r"[、。]", text) if part.strip()]
    if not chunks:
        chunks = [text]

    grammar_points: list[dict[str, str]] = []
    seen: set[str] = set()
    for surface, meaning, explanation in _GENERAL_PATTERN_CATALOG:
        if surface in compact and surface not in seen:
            seen.add(surface)
            grammar_points.append(
                {
                    "pattern": f"～{surface}",
                    "meaning_zh": meaning,
                    "explanation_zh": explanation,
                    "snippet": _extract_snippet_around(text, surface),
                }
            )
        if len(grammar_points) >= max(1, max_points):
            break

    if not grammar_points:
        inferred = _infer_basic_sentence_pattern(text)
        grammar_points.append(
            {
                "pattern": inferred["pattern"],
                "meaning_zh": inferred["meaning_zh"],
                "explanation_zh": inferred["explanation_zh"],
                "snippet": text,
            }
        )

    return {
        "summary_zh": "此句未命中文法庫對照，以下為離線一般文法解析（codex）。",
        "chunks": chunks[:4],
        "grammar_points": grammar_points[: max(1, max_points)],
        "steps": [
            "先找句尾型態（如 ます/です/た/ない）判斷語氣與時態。",
            "再看主要動詞前後是否有目的、原因、能力等文法標記。",
            "最後把分段語意合併回整句，確認邏輯順序是否自然。",
        ],
        "source": "codex_offline_sentence_v1",
    }


def _infer_basic_sentence_pattern(text: str) -> dict[str, str]:
    stripped = text.rstrip("。")
    if stripped.endswith("です"):
        return {
            "pattern": "～です",
            "meaning_zh": "是…（禮貌敘述）",
            "explanation_zh": "以禮貌體做客觀敘述，常見於說明狀態或判斷。",
        }
    if stripped.endswith("ます"):
        return {
            "pattern": "～ます",
            "meaning_zh": "…（禮貌動詞）",
            "explanation_zh": "以禮貌體描述動作，語氣中性且正式。",
        }
    if stripped.endswith("た"):
        return {
            "pattern": "～た",
            "meaning_zh": "…了（過去）",
            "explanation_zh": "表示已完成或過去發生的動作、狀態。",
        }
    if stripped.endswith("ない"):
        return {
            "pattern": "～ない",
            "meaning_zh": "不…（否定）",
            "explanation_zh": "否定表現，表示動作未發生或不成立。",
        }
    return {
        "pattern": "基本句型",
        "meaning_zh": "主語＋述語",
        "explanation_zh": "可先抓主語與主要動詞，再看補語或修飾成分來理解句意。",
    }


def _extract_snippet_around(sentence: str, surface: str, window: int = 14) -> str:
    pos = sentence.find(surface)
    if pos < 0:
        return sentence
    start = max(0, pos - window)
    end = min(len(sentence), pos + len(surface) + window)
    return sentence[start:end]
