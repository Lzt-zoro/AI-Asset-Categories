"""Controlled tag vocabulary with alias normalization.

Every tag the UI shows or a search filter uses comes from this module, so
synonyms such as 近景/特写 collapse into one canonical tag per dimension.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Iterable

DIMENSIONS: tuple[str, ...] = (
    "分类",
    "人物",
    "景别",
    "场景",
    "动作",
    "产品",
    "情绪",
    "素材类型",
    # Auxiliary dimensions: kept for existing data and manual tagging only.
    # They are no longer sent to the model, which trims prompt cost and noise.
    "光线",
    "色彩",
    "构图",
)

# Dimensions the model is asked about. Everything else is manual-only.
# ``分类`` now contains both folder labels and semantic content labels.
AI_DIMENSIONS: tuple[str, ...] = (
    "分类",
    "人物",
    "景别",
    "场景",
    "动作",
    "产品",
    "情绪",
)

# ``分类`` mirrors the client's folder taxonomy, so its values are open-ended.
OPEN_DIMENSIONS: frozenset[str] = frozenset({"分类"})

# These are semantic categories rather than folder names. They are kept in
# the same open ``分类`` dimension so directory labels and AI labels can be
# searched together without forcing a second taxonomy.
DEFAULT_CATEGORY_ALIASES: dict[str, str] = {
    "痛点": "疼点",
    "痛点使用": "疼点使用",
    "痛点涂抹": "疼点涂抹",
    "痛点呈现": "疼点呈现",
}

# canonical -> aliases. Aliases only need to be listed once and are matched
# after normalization; the canonical value is always stored in SQLite.
VOCABULARY: dict[str, dict[str, list[str]]] = {
    "人物": {
        "女性": ["女生", "女人", "女士", "女孩", "woman", "female", "女模特"],
        "男性": ["男生", "男人", "男士", "男孩", "man", "male", "男模特"],
        "多人": ["双人", "两个人", "情侣", "人群", "多人出镜"],
        "无人": ["空镜", "无人出镜", "场景空镜"],
        "手部": ["手", "双手", "手持", "手部特写"],
        "儿童": ["小孩", "孩子", "儿童模特"],
        "老人": ["老年人", "长者"],
    },
    "景别": {
        "特写": ["大特写", "close-up", "cu", "局部特写"],
        "近景": ["半身", "近距", "medium close-up", "mcu"],
        "中景": ["半身全景", "medium shot", "ms"],
        "全景": ["全身", "full shot", "fs", "wide shot"],
        "远景": ["大远景", "环境镜", "establishing shot"],
        "微距": ["macro", "产品微距"],
    },
    "场景": {
        "室内": ["房间", "居家", "家里", "室内环境", "indoor"],
        "卧室": ["床上", "床头", "卧室场景"],
        "浴室": ["卫生间", "洗手间", "淋浴间", "bathroom"],
        "客厅": ["沙发", "客厅场景"],
        "办公室": ["工位", "办公桌", "office"],
        "户外": ["室外", "街边", "室外环境", "outdoor"],
        "影棚": ["棚拍", "白色背景", "纯色背景", "studio"],
        "试衣间": ["更衣室", "换衣间"],
        "车内": ["汽车内", "车里"],
        "厨房": ["灶台", "厨房场景"],
        "纯色背景": ["白底", "灰底", "黑底", "clean background"],
    },
    "动作": {
        "涂抹": ["涂产品", "擦产品", "上脸", "抹开", "涂开", "擦拭", "apply", "涂抹动作"],
        "展示产品": ["举产品", "拿产品", "产品展示", "show product"],
        "打开包装": ["拆包装", "开箱", "拆盒", "unbox"],
        "挤压": ["按压", "挤膏体", "挤出来", "squeeze"],
        "拍打": ["轻拍", "拍脸", "pat"],
        "按摩": ["打圈", "揉搓", "massage"],
        "对比": ["前后对比", "使用前后", "before after", "对比展示"],
        "走动": ["走路", "走向镜头", "walk"],
        "转身": ["回头", "转圈", "turn around"],
        "说话": ["口播", "讲解", "对镜头说话", "talking"],
        "微笑": ["笑", "笑容", "smile"],
        "照镜子": ["对镜", "镜前", "mirror"],
        "使用产品": ["用产品", "体验产品", "使用中", "using product"],
        "整理衣物": ["拉衣角", "整理衣服", "adjust clothing"],
        "指向": ["手指", "指向产品", "point"],
    },
    "产品": {
        "祛痘霜": ["小蓝瓶", "祛痘膏", "痘痘膏", "acne cream"],
        "湿敷棉片": ["棉片", "湿敷片", "棉贴", "acne patch"],
        "护肤品": ["化妆品", "护肤产品", "skincare"],
        "无产品": ["没有产品", "无产品出镜", "no product"],
    },
    "分类": {
        "疼点": ["痛点", "问题肌肤", "痘痘问题"],
        "疼点使用": ["痛点使用", "疼点场景使用", "边疼点边用"],
        "疼点涂抹": ["痛点涂抹", "边疼点边涂"],
        "开场钩子": ["开头", "前3秒", "吸引注意", "hook", "opening"],
        "疼点呈现": ["问题展示", "痘痘特写", "problem", "痛点呈现"],
        "产品引入": ["产品出场", "介绍产品", "product intro"],
        "使用演示": ["演示", "用法展示", "demo", "usage"],
        "效果对比": ["效果", "对比", "result", "before after result"],
        "信任背书": ["资质", "检测报告", "口碑", "trust"],
        "行动号召": ["结尾引导", "cta", "引导下单", "购买引导"],
        "转场": ["过渡", "空镜转场", "transition"],
        "氛围铺垫": ["情绪铺垫", "氛围", "mood"],
        "美好展示": ["美好", "变美展示", "beauty shot", "成果展示"],
        "洗脸后展示": ["洁面后", "洗完脸展示"],
        "好坏皮肤对比": ["前后皮肤对比", "皮肤对比"],
    },
    "素材类型": {
        "普通素材": ["普通片段", "素材"],
        "成片": ["完整成片", "广告成片"],
    },
    "情绪": {
        "开心": ["高兴", "愉悦", "笑容", "happy"],
        "焦虑": ["烦恼", "担忧", "苦恼", "anxious"],
        "自信": ["笃定", "自信展示", "confident"],
        "惊讶": ["惊喜", "意外", "surprised"],
        "放松": ["舒适", "惬意", "relaxed"],
        "中性": ["平静", "无表情", "neutral"],
    },
    "光线": {
        "自然光": ["日光", "窗光", "natural light"],
        "柔光": ["柔光灯", "softbox", "soft light"],
        "硬光": ["直射光", "hard light"],
        "逆光": ["背光", "backlight"],
        "暖光": ["暖色光", "warm light"],
        "冷光": ["冷色光", "cool light"],
        "低照度": ["暗光", "偏暗", "low light"],
    },
    "色彩": {
        "高饱和": ["鲜艳", "浓彩", "vivid"],
        "低饱和": ["淡雅", "低彩度", "muted"],
        "暖色调": ["偏暖", "暖色", "warm tone"],
        "冷色调": ["偏冷", "冷色", "cool tone"],
        "黑白": ["单色", "monochrome"],
        "蓝色调": ["蓝调", "blue tone"],
    },
    "构图": {
        "居中构图": ["中心构图", "对称构图", "centered"],
        "三分法": ["九宫格", "rule of thirds"],
        "留白": ["大面积留白", "negative space"],
        "产品居中": ["产品中心", "product centered"],
        "前景遮挡": ["前景", "遮挡构图", "foreground"],
        "俯拍": ["俯视", "top down"],
        "平视": ["水平视角", "eye level"],
        "仰拍": ["仰视", "low angle"],
    },
}


def taxonomy_prompt(extra_categories: list[str] | None = None) -> str:
    """Prompt fragment listing every dimension the model may answer."""
    lines = []
    for dimension in AI_DIMENSIONS:
        # Folder categories are synchronized separately; only semantic
        # category choices are sent to the model, not arbitrary folder paths.
        if dimension == "分类":
            dimension_label = "分类"
        else:
            dimension_label = dimension
        entries = VOCABULARY.get(dimension, {})
        if dimension in OPEN_DIMENSIONS:
            names = list(entries)
            # Folder paths are synchronized separately and are not sent to
            # the model. Only configured semantic category labels are added.
            for name in extra_categories or []:
                if "/" not in name and name not in names:
                    names.append(name)
            body = "、".join(names[:80]) if names else "（按画面判断，可留空）"
        else:
            body = "、".join(entries)
        lines.append(f"- {dimension_label}: {body}")
    return "\n".join(lines)


# Kept for callers that only need the closed vocabulary.
TAXONOMY_PROMPT = taxonomy_prompt()


def clean_category(value: str) -> str:
    """Normalize one folder-derived category path."""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:80]


def clean_category_path(
    value: str,
    aliases: dict[str, str] | None = None,
) -> str:
    """Normalize a hierarchical category and unify the approved 痛/疼 names."""
    merged_aliases = dict(DEFAULT_CATEGORY_ALIASES)
    merged_aliases.update(aliases or {})
    # User aliases are allowed to override the default spelling for one-off
    # migrations; an empty config uses the approved 痛 -> 疼 normalization.
    parts: list[str] = []
    for raw in re.split(r"\s*/\s*", str(value)):
        part = clean_category(raw)
        part = merged_aliases.get(part, part)
        # The project-wide canonical spelling is always 疼, even if an old
        # local alias file still contains the previous 痛 spelling.
        part = DEFAULT_CATEGORY_ALIASES.get(part, part)
        part = part.replace("痛", "疼")
        if part and (not parts or parts[-1] != part):
            parts.append(part)
    return " / ".join(parts)


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return re.sub(r"[\s_\-/，,。.、;；:：]+", "", text)


_NORMALIZED: dict[str, tuple[str, str]] = {}
for _dimension, _entries in VOCABULARY.items():
    for _canonical, _aliases in _entries.items():
        for _word in [_canonical, *_aliases]:
            _NORMALIZED[normalize(_word)] = (_dimension, _canonical)


def resolve(value: str, dimension: str | None = None) -> tuple[str, str] | None:
    """Return ``(dimension, canonical)`` or ``None`` when unknown."""
    if dimension in {"剧情用途", "画面用途"}:
        dimension = "分类"
    if dimension in OPEN_DIMENSIONS:
        key = normalize(value)
        found = _NORMALIZED.get(key)
        if found and found[0] == "分类":
            return found
        category = clean_category_path(value)
        return (dimension, category) if category else None
    key = normalize(value)
    if not key:
        return None
    found = _NORMALIZED.get(key)
    if found and (dimension is None or found[0] == dimension):
        return found
    return None


def canonical_names(dimension: str) -> list[str]:
    return list(VOCABULARY.get(dimension, {}))


def dimensions() -> dict[str, list[str]]:
    return {dimension: list(entries) for dimension, entries in VOCABULARY.items()}


def ensure_vocabulary(conn: sqlite3.Connection) -> None:
    for dimension, entries in VOCABULARY.items():
        for canonical in entries:
            conn.execute(
                """
                INSERT INTO tags(dimension, name, canonical)
                VALUES (?, ?, ?)
                ON CONFLICT(dimension, canonical) DO NOTHING
                """,
                (dimension, canonical, canonical),
            )


def tag_id(conn: sqlite3.Connection, dimension: str, canonical: str) -> int:
    row = conn.execute(
        """
        SELECT id FROM tags
        WHERE dimension=? AND canonical=?
        ORDER BY length(name)
        LIMIT 1
        """,
        (dimension, canonical),
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO tags(dimension, name, canonical) VALUES (?, ?, ?)",
            (dimension, canonical, canonical),
        )
        return int(cursor.lastrowid)
    return int(row["id"])


def attach(
    conn: sqlite3.Connection,
    clip_id: str,
    items: Iterable[tuple[str, str]],
    *,
    confidence: float = 1.0,
    origin: str = "auto",
) -> int:
    count = 0
    for dimension, canonical in items:
        identifier = tag_id(conn, dimension, canonical)
        conn.execute(
            """
            INSERT INTO clip_tags(clip_id, tag_id, confidence, origin)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(clip_id, tag_id) DO UPDATE SET
                confidence=excluded.confidence,
                origin=CASE
                    WHEN clip_tags.origin='manual' THEN 'manual'
                    WHEN clip_tags.origin='folder' THEN 'folder'
                    ELSE excluded.origin
                END
            """,
            (clip_id, identifier, float(confidence), origin),
        )
        count += 1
    return count
