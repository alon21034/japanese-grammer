from __future__ import annotations

import json
import logging
import time
from threading import Lock
from typing import Any

import requests

RESPONSES_URL = "https://api.openai.com/v1/responses"
# Use uvicorn.error logger so logs appear in server stdout/stderr by default.
_logger = logging.getLogger("uvicorn.error")
_LOG_CONTENT_MAX_CHARS = 12000
_logical_call_lock = Lock()
_http_call_lock = Lock()
_logical_call_count = 0
_http_call_count = 0


def _next_logical_call_id() -> int:
    global _logical_call_count
    with _logical_call_lock:
        _logical_call_count += 1
        return _logical_call_count


def _next_http_call_id() -> int:
    global _http_call_count
    with _http_call_lock:
        _http_call_count += 1
        return _http_call_count


def _truncate_for_log(text: str, max_len: int = _LOG_CONTENT_MAX_CHARS) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _serialize_input_for_log(payload: dict[str, Any]) -> str:
    input_block = payload.get("input", [])
    try:
        raw = json.dumps(input_block, ensure_ascii=False)
    except Exception:
        raw = str(input_block)
    return _truncate_for_log(raw)


class OpenAIClient:
    def __init__(self, api_key: str | None, model: str) -> None:
        self.api_key = api_key
        self.model = model

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.model)

    def generate_lesson_payload(
        self,
        title: str,
        article_text: str,
        grammar_references: list[dict[str, str]] | None = None,
        timeout_sec: int = 60,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("OpenAI credentials missing. Set OPENAI_API_KEY and OPENAI_MODEL.")

        references_block = _build_references_block(grammar_references or [])

        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是日文教師。請根據 NHK Easy 日文新聞，為繁體中文使用者產生"
                        "1~3 個重點文法解說，以及 1~3 題練習。"
                        "練習需包含容易混淆文法，並盡量包含短答（日文輸入）題。"
                        "請優先參考提供的文法庫對照，不要自行發明不存在的文法定義。"
                        "所有中文輸出必須使用繁體中文（台灣常用字），禁止使用簡體字。"
                        "只輸出符合 schema 的 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"標題：{title}\n\n"
                        f"文章：\n{article_text}\n\n"
                        "請挑選文章中實際出現、適合初中級學習者的 1 到 3 個文法點。"
                        "例句必須引用或貼近文章原句。"
                        "題目數量 1 到 3 題，至少要有 1 題是易混淆文法題，"
                        "並優先加入短答題（例如翻譯成日文或填空）。\n\n"
                        "再次要求：中文欄位請全部使用繁體中文，不可出現簡體字。\n\n"
                        "文法庫對照（RAG檢索結果）：\n"
                        f"{references_block}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "nhk_grammar_lesson",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["grammar_points", "questions"],
                        "properties": {
                            "grammar_points": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "pattern",
                                        "meaning_zh",
                                        "explanation_zh",
                                        "example_from_article",
                                    ],
                                    "properties": {
                                        "pattern": {"type": "string"},
                                        "meaning_zh": {"type": "string"},
                                        "explanation_zh": {"type": "string"},
                                        "example_from_article": {"type": "string"},
                                    },
                                },
                            },
                            "questions": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "type",
                                        "question_zh",
                                        "question_ja",
                                        "options",
                                        "accepted_answers",
                                        "explanation_zh",
                                    ],
                                    "properties": {
                                        "type": {"type": "string", "enum": ["mcq", "short_answer"]},
                                        "question_zh": {"type": "string"},
                                        "question_ja": {"type": "string"},
                                        "options": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["A", "B", "C", "D"],
                                            "properties": {
                                                "A": {"type": "string"},
                                                "B": {"type": "string"},
                                                "C": {"type": "string"},
                                                "D": {"type": "string"},
                                            },
                                        },
                                        "accepted_answers": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 5,
                                            "items": {"type": "string"},
                                        },
                                        "explanation_zh": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "strict": True,
                }
            },
        }

        return self._request_json_payload(
            payload,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            parse_error_message="OpenAI returned non-JSON lesson content.",
            object_error_message="OpenAI lesson payload is not a JSON object.",
            operation_name="lesson_generation",
        )

    def explain_grammar_references(
        self,
        grammar_references: list[dict[str, str]],
        timeout_sec: int = 60,
        max_retries: int = 2,
    ) -> list[dict[str, str]]:
        if not self.is_configured:
            raise RuntimeError("OpenAI credentials missing. Set OPENAI_API_KEY and OPENAI_MODEL.")
        if not grammar_references:
            return []

        references_block = _build_detailed_references_block(grammar_references)
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是日文文法老師。"
                        "請根據每個文法點提供繁體中文的詳細說明，重點放在文法用法與例句解析。"
                        "所有中文輸出必須使用繁體中文（台灣常用字），禁止使用簡體字。"
                        "只輸出符合 schema 的 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "請逐條分析以下文法庫對照，並輸出詳細解釋。"
                        "再次要求：中文欄位請全部使用繁體中文，不可出現簡體字。\n\n"
                        f"{references_block}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "grammar_detailed_explanations",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["items"],
                        "properties": {
                            "items": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "pattern",
                                        "detailed_explanation_zh",
                                        "example_breakdown_zh",
                                        "common_confusion_zh",
                                    ],
                                    "properties": {
                                        "pattern": {"type": "string"},
                                        "detailed_explanation_zh": {"type": "string"},
                                        "example_breakdown_zh": {"type": "string"},
                                        "common_confusion_zh": {"type": "string"},
                                    },
                                },
                            }
                        },
                    },
                    "strict": True,
                }
            },
        }
        parsed = self._request_json_payload(
            payload,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            parse_error_message="OpenAI returned non-JSON grammar detail content.",
            object_error_message="OpenAI grammar detail payload is not a JSON object.",
            operation_name="grammar_explain",
        )
        raw_items = parsed.get("items", [])
        if not isinstance(raw_items, list):
            raise RuntimeError("OpenAI grammar detail payload is missing items.")

        items: list[dict[str, str]] = []
        for raw in raw_items[:3]:
            if not isinstance(raw, dict):
                continue
            pattern = str(raw.get("pattern", "")).strip()
            detailed = str(raw.get("detailed_explanation_zh", "")).strip()
            breakdown = str(raw.get("example_breakdown_zh", "")).strip()
            confusion = str(raw.get("common_confusion_zh", "")).strip()
            if not pattern:
                continue
            items.append(
                {
                    "pattern": pattern,
                    "detailed_explanation_zh": detailed,
                    "example_breakdown_zh": breakdown,
                    "common_confusion_zh": confusion,
                }
            )
        if not items:
            raise RuntimeError("OpenAI returned empty grammar detail items.")
        return items

    def _request_json_payload(
        self,
        payload: dict[str, Any],
        *,
        timeout_sec: int,
        max_retries: int,
        parse_error_message: str,
        object_error_message: str,
        operation_name: str,
    ) -> dict[str, Any]:
        logical_id = _next_logical_call_id()
        model_name = str(payload.get("model", "")).strip()
        _logger.info(
            "[OpenAI] start logical_id=%s op=%s model=%s timeout=%ss max_retries=%s input=%s",
            logical_id,
            operation_name,
            model_name,
            timeout_sec,
            max_retries,
            _serialize_input_for_log(payload),
        )
        attempts = max(1, max_retries + 1)
        response: requests.Response | None = None
        for attempt in range(1, attempts + 1):
            http_id = _next_http_call_id()
            try:
                _logger.info(
                    "[OpenAI] request http_id=%s logical_id=%s attempt=%s/%s op=%s POST %s",
                    http_id,
                    logical_id,
                    attempt,
                    attempts,
                    operation_name,
                    RESPONSES_URL,
                )
                response = requests.post(
                    RESPONSES_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout_sec,
                )
            except requests.RequestException as exc:
                _logger.warning(
                    "[OpenAI] network_error http_id=%s logical_id=%s attempt=%s/%s op=%s error=%s",
                    http_id,
                    logical_id,
                    attempt,
                    attempts,
                    operation_name,
                    str(exc),
                )
                if attempt < attempts:
                    time.sleep(1.5 * attempt)
                    continue
                raise RuntimeError("OpenAI request failed. Please retry later.") from exc

            _logger.info(
                "[OpenAI] response http_id=%s logical_id=%s attempt=%s/%s op=%s status=%s",
                http_id,
                logical_id,
                attempt,
                attempts,
                operation_name,
                response.status_code,
            )
            if response.status_code == 429:
                if attempt < attempts:
                    wait_sec = _retry_after_seconds(response) or (1.5 * attempt)
                    _logger.warning(
                        "[OpenAI] rate_limit http_id=%s logical_id=%s attempt=%s/%s retry_in=%.2fs",
                        http_id,
                        logical_id,
                        attempt,
                        attempts,
                        wait_sec,
                    )
                    time.sleep(wait_sec)
                    continue
                _logger.error(
                    "[OpenAI] rate_limit_exhausted logical_id=%s op=%s",
                    logical_id,
                    operation_name,
                )
                raise RuntimeError("OpenAI rate limit reached (429). Please retry in 1-2 minutes.")

            if 500 <= response.status_code < 600:
                if attempt < attempts:
                    _logger.warning(
                        "[OpenAI] server_error http_id=%s logical_id=%s attempt=%s/%s status=%s",
                        http_id,
                        logical_id,
                        attempt,
                        attempts,
                        response.status_code,
                    )
                    time.sleep(1.5 * attempt)
                    continue
                _logger.error(
                    "[OpenAI] server_error_exhausted logical_id=%s op=%s status=%s",
                    logical_id,
                    operation_name,
                    response.status_code,
                )
                raise RuntimeError(f"OpenAI service unavailable ({response.status_code}). Please retry later.")
            break

        if response is None:
            raise RuntimeError("OpenAI request failed before receiving any response.")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = _extract_error_message(response)
            if detail:
                raise RuntimeError(f"OpenAI request failed ({response.status_code}): {detail}") from exc
            raise RuntimeError(f"OpenAI request failed ({response.status_code}).") from exc

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            _logger.error(
                "[OpenAI] invalid_json logical_id=%s op=%s status=%s",
                logical_id,
                operation_name,
                response.status_code,
            )
            raise RuntimeError("OpenAI response is not valid JSON.") from exc
        text = _extract_output_text(data)
        _logger.info(
            "[OpenAI] output logical_id=%s op=%s output_text=%s",
            logical_id,
            operation_name,
            _truncate_for_log(text),
        )
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            _logger.error(
                "[OpenAI] output_not_json logical_id=%s op=%s",
                logical_id,
                operation_name,
            )
            raise RuntimeError(parse_error_message) from exc
        if not isinstance(parsed, dict):
            _logger.error(
                "[OpenAI] output_not_object logical_id=%s op=%s",
                logical_id,
                operation_name,
            )
            raise RuntimeError(object_error_message)
        _logger.info(
            "[OpenAI] done logical_id=%s op=%s",
            logical_id,
            operation_name,
        )
        return parsed


def _extract_output_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts: list[str] = []
    output = data.get("output", [])
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    if texts:
        return "\n".join(texts)
    raise RuntimeError("OpenAI response did not include output text.")


def _retry_after_seconds(response: requests.Response) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(value, 30.0))


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if not isinstance(err, dict):
        return ""
    msg = err.get("message")
    return str(msg).strip() if isinstance(msg, str) else ""


def _build_references_block(grammar_references: list[dict[str, str]]) -> str:
    if not grammar_references:
        return "- （無）"

    lines: list[str] = []
    for idx, ref in enumerate(grammar_references[:3], start=1):
        pattern = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        level = str(ref.get("level", "")).strip()
        meaning = str(ref.get("meaning", "")).strip()
        source = str(ref.get("url", "")).strip()
        matched = str(ref.get("matched_terms", "")).strip()
        head = f"{idx}. {pattern}"
        if level:
            head += f" [{level}]"
        lines.append(head)
        if meaning:
            lines.append(f"   - 意味: {meaning}")
        if matched:
            lines.append(f"   - 關聯詞: {matched}")
        if source:
            lines.append(f"   - 來源: {source}")
    return "\n".join(lines)


def _build_detailed_references_block(grammar_references: list[dict[str, str]]) -> str:
    if not grammar_references:
        return "- （無）"

    lines: list[str] = []
    for idx, ref in enumerate(grammar_references[:3], start=1):
        pattern = str(ref.get("pattern", "")).strip() or str(ref.get("title", "")).strip()
        meaning = str(ref.get("meaning", "")).strip()
        explanation = str(ref.get("explanation", "")).strip()
        example = str(ref.get("example", "")).strip()
        example_kanji = str(ref.get("example_kanji", "")).strip()
        head = f"{idx}. {pattern}"
        lines.append(head)
        if meaning:
            lines.append(f"   - 意思: {meaning}")
        if explanation:
            lines.append(f"   - 既有解釋: {explanation}")
        if example:
            lines.append(f"   - 例句: {example}")
        if example_kanji:
            lines.append(f"   - 例句漢字標註: {example_kanji}")
    return "\n".join(lines)
