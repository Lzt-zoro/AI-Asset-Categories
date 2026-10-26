from __future__ import annotations

from material_library import tags
from material_library.labeling import sanitize


def test_sanitize_keeps_only_vocabulary_tags():
    description, resolved, confidence = sanitize({
        "description": "女生在浴室涂抹面霜",
        "tags": {
            "景别": ["近景", "不存在的词"],
            "动作": ["涂抹", "抹开"],
            "场景": "浴室",
            "无关维度": ["随便"],
        },
        "confidence": 1.4,
    })
    assert description == "女生在浴室涂抹面霜"
    assert resolved["景别"] == ["近景"]
    assert resolved["动作"] == ["涂抹"]
    assert resolved["场景"] == ["浴室"]
    assert "任意" not in resolved
    assert confidence == 1.0


def test_sanitize_accepts_flat_tag_list():
    _, resolved, confidence = sanitize({
        "description": "产品特写",
        "tags": ["近景", "展示产品"],
        "confidence": 0.7,
    })
    assert resolved == {"景别": ["近景"], "动作": ["展示产品"]}
    assert confidence == 0.7


def test_sanitize_handles_missing_fields():
    description, resolved, confidence = sanitize({})
    assert description == ""
    assert resolved == {}
    assert confidence == 0.5


def test_model_prompt_uses_semantic_category_without_folder_paths():
    """AI gets semantic categories, not arbitrary folder paths."""
    prompt = tags.taxonomy_prompt(["痛点使用 / 痛点涂抹"])
    assert "分类" in prompt
    assert "痛点涂抹" not in prompt
    for dimension in ("分类", "人物", "景别", "场景", "动作", "产品", "情绪"):
        assert dimension in prompt
    for manual_only in ("光线", "色彩", "构图"):
        assert manual_only not in prompt
