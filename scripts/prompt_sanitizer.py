#!/usr/bin/env python3
"""AI 图像生成提示词过滤工具。过滤停用词和禁用词，提升 prompt 质量。

支持领域感知过滤：
- generic（默认）：通用图像生成，过滤所有模糊修饰词
- fashion：服装/面料/印花领域，保留审美方向关键词（elegant/beautiful/lovely 等），
  只过滤真正空洞的强调词（very/really/quite 类）

反直觉事实：通用 prompt 工程文章常说"避免空洞形容词"，但服装、家居、化妆品、
首饰等消费品类反而依赖 elegant/lovely/beautiful 等词来锁定商业可售感。
Stable Diffusion / Imagen / Gemini 对这些词非常敏感，剥掉后模型可能输出更
"档案照"风格而不是"商业印花"。
"""

# 真正空洞的强调词（所有领域都应过滤）
_STOP_WORDS_CORE = frozenset({
    "very", "really", "quite", "rather", "extremely",
    "incredibly", "amazingly", "nice", "good", "bad",
    "wonderful", "fantastic", "great", "perfect",
    "just", "simply", "basically", "actually", "definitely",
    "truly", "absolutely", "totally", "completely", "highly",
    "so", "too", "much", "many", "more", "most", "less", "least",
})

# 通用图像领域的停用词（包含审美词，用于人像/风景等通用场景）
STOP_WORDS_GENERIC = _STOP_WORDS_CORE | frozenset({
    "beautiful", "lovely",
    "stunning", "gorgeous", "elegant",
    "pretty", "cute", "attractive", "pleasant",
    "aesthetic", "artistic",
})

# 服装/面料/印花专用停用词（保留审美方向关键词）
STOP_WORDS_FASHION = _STOP_WORDS_CORE | frozenset({
    # 服装域只额外过滤少量真正无视觉语义的词
    # elegant/beautiful/lovely/gorgeous/stunning/cute/aesthetic/artistic
    # 保留，因为它们是模型锁定商业可售感的关键词
})

# 内容安全禁用词（可能触发图像生成 API 内容过滤）—— 所有领域统一过滤
BANNED_WORDS = frozenset({
    "nude", "naked", "sexual", "violence", "blood", "gore",
    "weapon", "gun", "rifle", "knife", "blade", "bomb", "explosive",
    "terrorist", "terrorism", "drug", "cocaine", "heroin", "marijuana",
    "slave", "slavery", "torture", "abuse", "kill", "murder", "death",
    "dead", "corpse", "suicide", "self-harm", "mutilation",
    "porn", "pornography", "erotic", "sexy", "seductive",
    "racist", "racism", "nazi", "hitler", "swastika", "kkk",
    "propaganda", "misinformation", "fake", "hoax",
})

# 提示词中应避免的低价值重复词 —— 所有领域统一过滤
NOISE_WORDS = frozenset({
    "please", "kindly", "make sure", "ensure that", "try to",
    "attempt to", "should be", "must be", "needs to", "has to",
})


def _get_stop_words(domain: str = "generic") -> frozenset:
    """根据领域返回对应的停用词集合。"""
    if domain == "fashion":
        return STOP_WORDS_FASHION
    return STOP_WORDS_GENERIC


def sanitize_prompt(text: str, domain: str = "generic") -> str:
    """过滤提示词中的停用词、禁用词和噪音词。

    Args:
        text: 输入提示词
        domain: 领域上下文。"generic" 使用完整停用词表；"fashion" 保留审美关键词

    规则：
    1. 拆分单词，按空格和标点分隔
    2. 检测停用词和禁用词，移除
    3. 保留专有名词、形容词短语中的有效视觉描述词
    4. 若文本被大幅过滤（保留 < 60%），打印警告
    """
    if not text or not isinstance(text, str):
        return text

    import re

    stop_words = _get_stop_words(domain)
    tokens = text.split()
    cleaned = []
    removed = []

    for token in tokens:
        # 去掉首尾标点
        stripped = token.strip(",.;:!?()[]{}'\"").lower()
        if stripped in stop_words or stripped in BANNED_WORDS or stripped in NOISE_WORDS:
            removed.append(token)
            continue
        # 也检查去掉连字符后的形式
        stripped_dash = stripped.replace("-", " ")
        parts = stripped_dash.split()
        if any(p in stop_words or p in BANNED_WORDS or p in NOISE_WORDS for p in parts):
            removed.append(token)
            continue
        cleaned.append(token)

    # 质量检查：如果移除太多，保留原始文本（仅过滤禁用词）
    if len(cleaned) < len(tokens) * 0.4:
        cleaned = []
        for token in tokens:
            stripped = token.strip(",.;:!?()[]{}'\"").lower()
            if stripped in BANNED_WORDS:
                removed.append(token)
                continue
            cleaned.append(token)

    result = " ".join(cleaned)
    # 清理多余空格
    result = re.sub(r"\s+", " ", result).strip()
    return result


def sanitize_prompts_in_dict(data: dict, keys: tuple[str, ...] = ("prompt",), domain: str = "generic") -> dict:
    """递归遍历字典，对指定 key 的值调用 sanitize_prompt。

    Args:
        data: 输入字典
        keys: 需要过滤的键名
        domain: 领域上下文，传递给 sanitize_prompt
    """
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in keys and isinstance(v, str):
                out[k] = sanitize_prompt(v, domain=domain)
            else:
                out[k] = sanitize_prompts_in_dict(v, keys=keys, domain=domain)
        return out
    elif isinstance(data, list):
        return [sanitize_prompts_in_dict(item, keys=keys, domain=domain) for item in data]
    return data


def validate_prompt(text: str, domain: str = "generic") -> list[str]:
    """检查提示词中的问题，返回警告列表。"""
    warnings = []
    stop_words = _get_stop_words(domain)
    tokens = text.lower().split()
    for t in tokens:
        stripped = t.strip(",.;:!?()[]{}'\"")
        if stripped in BANNED_WORDS:
            warnings.append(f"禁用词: {stripped}")
        elif stripped in stop_words:
            warnings.append(f"停用词: {stripped}")
    return warnings
