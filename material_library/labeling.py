"""AI labeling through an OpenAI-compatible multimodal endpoint.

The provider only sees a few sampled frames plus timing metadata. It never
receives the full video, and failures degrade to ``needs_review`` instead of
blocking ingest.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import tags
from .config import AIConfig


class LabelingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClipContext:
    source_name: str
    source_duration: float
    start: float
    end: float
    frames: list[Path] = field(default_factory=list)
    transcript: str = ""
    category: str = ""
    categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LabelResult:
    description: str
    tags: dict[str, list[str]]
    confidence: float
    provider: str
    model: str


class LabelProvider(Protocol):
    name: str

    def label(self, context: ClipContext) -> LabelResult: ...


SYSTEM_PROMPT = """你是中文视频素材标注师。根据给定帧和片段信息输出结构化标签，供电商短视频剪辑检索使用。

规则：
1. 只描述画面能支持的事实，不猜测品牌、功效、价格或人物身份。
2. 标签必须从给定词表中选择，不得自造。每个维度最多 2 个标签，无关维度可省略。
3. 「分类」可以根据画面和文案选择语义分类；已有文件夹分类只是来源信息，不要把它改写成别的目录。
4. 景别、人物、动作、场景基于画面；分类基于画面用途和片段在广告中的可能位置。
5. description 用一句中文描述"谁在什么场景做了什么"，40 字以内。
6. 输出 JSON，不要 Markdown。

可选词表：
{taxonomy}

输出格式：
{{"description": "...", "tags": {{"景别": ["近景"], "动作": ["涂抹"]}}, "confidence": 0.8}}
"""


def _json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise LabelingError("模型未返回 JSON")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise LabelingError("模型返回值不是对象")
    return value


def sanitize(
    raw: dict[str, Any],
    extra_categories: list[str] | None = None,
) -> tuple[str, dict[str, list[str]], float]:
    description = str(raw.get("description") or "").strip()[:200]
    result: dict[str, list[str]] = {}
    raw_tags = raw.get("tags")
    items: list[tuple[str, Any]] = []
    if isinstance(raw_tags, dict):
        for dimension, values in raw_tags.items():
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                items.extend((str(dimension), value) for value in values)
    elif isinstance(raw_tags, list):
        for value in raw_tags:
            items.append(("", value))
    for dimension, value in items:
        if dimension in {"剧情用途", "画面用途"}:
            dimension = "分类"
        resolved = tags.resolve(str(value), dimension if dimension in tags.VOCABULARY else None)
        if resolved is None:
            continue
        dimension_name, canonical = resolved
        if dimension_name == "分类":
            allowed = set(tags.VOCABULARY.get("分类", {}))
            allowed.update(tags.clean_category_path(item) for item in extra_categories or [])
            if canonical not in allowed:
                # Folder paths are attached by the library. Custom semantic
                # labels are accepted only when explicitly configured.
                continue
        bucket = result.setdefault(dimension_name, [])
        if canonical not in bucket and len(bucket) < 2:
            bucket.append(canonical)
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return description, result, max(0.0, min(1.0, confidence))


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, config: AIConfig) -> None:
        self.config = config

    def _payload(self, context: ClipContext) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"素材名：{context.source_name}\n"
                    f"原片时长：{context.source_duration:.2f}s\n"
                    f"片段区间：{context.start:.2f}s - {context.end:.2f}s"
                    f"（{context.end - context.start:.2f}s）\n"
                    f"已有分类：{context.category or '无'}\n"
                    f"台词/字幕：{context.transcript or '无'}\n"
                    "以下按时间顺序给出该片段内的抽帧。"
                ),
            }
        ]
        for index, frame in enumerate(context.frames[: self.config.max_frames]):
            encoded = base64.b64encode(Path(frame).read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"帧 {index + 1}"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"},
            })
        return {
            "model": self.config.model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        taxonomy=tags.taxonomy_prompt(context.categories)
                    ),
                },
                {"role": "user", "content": content},
            ],
        }

    def label(self, context: ClipContext) -> LabelResult:
        if not self.config.configured:
            raise LabelingError("AI 未启用或未配置")
        url = f"{self.config.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = self._payload(context)
        data = self._post_with_retry(url, headers, payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LabelingError("AI 响应结构不符合 chat/completions 协议") from None
        description, resolved, confidence = sanitize(
            _json_object(text), context.categories
        )
        return LabelResult(
            description=description,
            tags=resolved,
            confidence=confidence,
            provider=self.name,
            model=self.config.model,
        )

    def _post_with_retry(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Retry transient failures; a dropped connection should not cost a clip.

        Client errors (400/401/403/404) are permanent and fail immediately.
        """
        attempts = max(1, int(self.config.retries) + 1)
        last: str = ""
        for index in range(attempts):
            try:
                with httpx.Client(timeout=self.config.timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                if response.status_code >= 500 or response.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code if error.response is not None else 0
                body = error.response.text[:200] if error.response is not None else ""
                last = f"AI 接口返回 {status}: {body}"
                if status < 500 and status != 429:
                    raise LabelingError(last) from None
            except httpx.HTTPError as error:
                last = f"AI 接口请求失败: {type(error).__name__}"
            except ValueError:
                last = "AI 响应不是合法 JSON"
            if index + 1 < attempts:
                time.sleep(min(2.0 * (index + 1), 6.0))
        raise LabelingError(last or "AI 接口请求失败")


class ManualProvider:
    """Used when AI is disabled; produces a reviewable placeholder."""

    name = "manual"

    def label(self, context: ClipContext) -> LabelResult:
        return LabelResult(
            description="",
            tags={},
            confidence=0.0,
            provider=self.name,
            model="",
        )


def build_provider(config: AIConfig) -> LabelProvider:
    if config.configured:
        return OpenAICompatibleProvider(config)
    return ManualProvider()
