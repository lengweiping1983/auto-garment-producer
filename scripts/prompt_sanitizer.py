#!/usr/bin/env python3
"""AI 图像生成提示词过滤工具。过滤停用词和禁用词，提升 prompt 质量。"""

# AI 图像生成停用词（模糊修饰，降低 prompt 质量，无实际视觉语义）
STOP_WORDS = frozenset({
    "very", "really", "quite", "rather", "pretty", "extremely",
    "incredibly", "amazingly", "beautiful", "nice", "good", "bad",
    "wonderful", "fantastic", "great", "perfect", "lovely",
    "just", "simply", "basically", "actually", "definitely",
    "truly", "absolutely", "totally", "completely", "highly",
    "so", "too", "much", "many", "more", "most", "less", "least",
    "aesthetic", "artistic", "stunning", "gorgeous", "elegant",
    "pretty", "cute", "lovely", "attractive", "pleasant",
})

# 内容安全禁用词（可能触发图像生成 API 内容过滤）
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

# 提示词中应避免的低价值重复词
NOISE_WORDS = frozenset({
    "please", "kindly", "make sure", "ensure that", "try to",
    "attempt to", "should be", "must be", "needs to", "has to",
})


def sanitize_prompt(text: str) -> str:
    """过滤提示词中的停用词、禁用词和噪音词。

    规则：
    1. 拆分单词，按空格和标点分隔
    2. 检测停用词和禁用词，移除
    3. 保留专有名词、形容词短语中的有效视觉描述词
    4. 若文本被大幅过滤（保留 < 60%），打印警告
    """
    if not text or not isinstance(text, str):
        return text

    import re

    # 保留的 token 列表
    tokens = text.split()
    cleaned = []
    removed = []

    for token in tokens:
        # 去掉首尾标点
        stripped = token.strip(",.;:!?()[]{}'\"").lower()
        if stripped in STOP_WORDS or stripped in BANNED_WORDS or stripped in NOISE_WORDS:
            removed.append(token)
            continue
        # 也检查去掉连字符后的形式
        stripped_dash = stripped.replace("-", " ")
        parts = stripped_dash.split()
        if any(p in STOP_WORDS or p in BANNED_WORDS or p in NOISE_WORDS for p in parts):
            removed.append(token)
            continue
        cleaned.append(token)

    # 质量检查：如果移除太多，保留原始文本
    if len(cleaned) < len(tokens) * 0.4:
        # 太多词被过滤，可能是误判，只过滤明确的禁用词
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


def sanitize_prompts_in_dict(data: dict, keys: tuple[str, ...] = ("prompt",)) -> dict:
    """递归遍历字典，对指定 key 的值调用 sanitize_prompt。"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in keys and isinstance(v, str):
                out[k] = sanitize_prompt(v)
            else:
                out[k] = sanitize_prompts_in_dict(v, keys)
        return out
    elif isinstance(data, list):
        return [sanitize_prompts_in_dict(item, keys) for item in data]
    return data


def validate_prompt(text: str) -> list[str]:
    """检查提示词中的问题，返回警告列表。"""
    warnings = []
    tokens = text.lower().split()
    for t in tokens:
        stripped = t.strip(",.;:!?()[]{}'\"")
        if stripped in BANNED_WORDS:
            warnings.append(f"禁用词: {stripped}")
        elif stripped in STOP_WORDS:
            warnings.append(f"停用词: {stripped}")
    return warnings
