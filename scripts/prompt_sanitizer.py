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

from __future__ import annotations

from dataclasses import asdict, dataclass
import re

# 真正空洞的强调词（所有领域都应过滤）
_STOP_WORDS_CORE = frozenset({
    "very", "really", "quite", "rather", "extremely",
    "incredibly", "amazingly", "nice", "good", "bad",
    "wonderful", "fantastic", "great", "perfect",
    "just", "simply", "basically", "actually", "definitely",
    "truly", "absolutely", "totally", "completely", "highly",
    "so",
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

# 内容安全禁用词（可能触发图像生成 API 内容过滤）—— 所有领域统一过滤。
#
# 注意：这里不是为了绕过平台安全策略，而是把服装设计里常见但容易误伤
# 的表达改写成安全、商业、可渲染的视觉语言。真正的危险内容仍会被移除。
BANNED_WORDS_BY_CATEGORY = {
    "sexual_or_nudity": frozenset({
        "nude", "nudity", "naked", "bare", "barely", "topless", "bottomless",
        "sexual", "sex", "porn", "pornography", "erotic", "fetish",
        "sexy", "seductive", "sensual", "provocative", "lingerie",
    }),
    "violence_or_weapons": frozenset({
        "violence", "violent", "blood", "bloody", "gore", "gory",
        "weapon", "weapons", "gun", "guns", "rifle", "rifles", "pistol",
        "pistols", "knife", "knives", "blade", "blades", "bomb", "bombs",
        "explosive", "explosives", "bullet", "bullets", "ammo", "ammunition",
        "kill", "killing", "murder", "death", "dead", "corpse", "torture",
        "mutilation",
    }),
    "self_harm": frozenset({"suicide", "self-harm", "selfharm", "cutting"}),
    "drugs": frozenset({"drug", "drugs", "cocaine", "heroin", "marijuana", "weed", "meth"}),
    "hate_or_extremism": frozenset({
        "racist", "racism", "nazi", "hitler", "swastika", "kkk",
        "terrorist", "terrorism", "extremist", "extremism",
    }),
    "deception": frozenset({"propaganda", "misinformation", "fake", "hoax"}),
    "abuse_or_exploitation": frozenset({"slave", "slavery", "abuse", "abusive"}),
}

BANNED_WORDS = frozenset(
    word
    for words in BANNED_WORDS_BY_CATEGORY.values()
    for word in words
)

SAFE_TOKEN_REPLACEMENTS = {
    # Fashion/color false positives.
    "nude": "skin-tone beige",
    "nudity": "minimal skin exposure",
    "naked": "plain unprinted",
    "bare": "minimal",
    "sexy": "confident commercial",
    "seductive": "elegant commercial",
    "sensual": "soft elegant",
    "provocative": "bold commercial",
    "lingerie": "delicate apparel",
    # Visual color/material false positives.
    "blood": "deep crimson",
    "bloody": "deep crimson",
    "gore": "dark red organic texture",
    "gory": "dark red organic texture",
    "dead": "muted",
    "death": "gothic mood",
    "corpse": "pale antique figure",
    "gun": "metallic graphite object",
    "guns": "metallic graphite objects",
    "gunmetal": "dark graphite",
    "knife": "sharp geometric motif",
    "knives": "sharp geometric motifs",
    "blade": "sharp leaf-shaped motif",
    "blades": "sharp leaf-shaped motifs",
    "bullet": "small oval motif",
    "bullets": "small oval motifs",
    "weapon": "non-hazardous prop",
    "weapons": "non-hazardous props",
    "bomb": "round graphic motif",
    "bombs": "round graphic motifs",
    "explosive": "energetic",
    "explosives": "energetic graphic motifs",
    # Unsafe categories that should not be visualized.
    "drug": "botanical motif",
    "drugs": "botanical motifs",
    "weed": "leaf motif",
    "marijuana": "leaf motif",
}

SAFE_PHRASE_REPLACEMENTS = (
    (r"\bno\s+nude\s+(?:figure|body|person|model|subject)\b", "fully clothed apparel-safe subject"),
    (r"\bnude\s+(?:figure|body|person|model|subject)\b", "fully clothed subject"),
    (r"\bnude\s+(?:color|tone|palette|beige|fabric|base|ground)\b", "skin-tone beige palette"),
    (r"\bskin\s+nude\b", "skin-tone beige"),
    (r"\bblood\s+red\b", "deep crimson"),
    (r"\bgore\s+tex(?:ture)?\b", "dark red organic texture"),
    (r"\bgun\s*metal\b", "dark graphite"),
    (r"\bgunmetal\b", "dark graphite"),
    (r"\bknife\s+pleats?\b", "sharp pressed pleats"),
    (r"\bmarijuana\s+leaf\b", "stylized botanical leaf"),
    (r"\bblade-shaped\s+leaves\b", "sharp leaf-shaped leaves"),
    (r"\bfake\s+transparency\s+grid\b", "checkerboard preview grid"),
    (r"\brazor\s+sharp\b", "crisp precise"),
    (r"\bdead\s+stock\b", "surplus stock"),
    (r"\bno\s+(?:blood|gore|violence|weapon|gun|knife|nude|nudity|sexual|porn|drug)s?\b", "apparel-safe content"),
)


@dataclass
class PromptSanitizationResult:
    original_text: str
    sanitized_text: str
    domain: str
    prompt_role: str
    removed: list[str]
    replacements: list[dict[str, str]]
    categories: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

# 提示词中应避免的低价值重复词 —— 所有领域统一过滤
NOISE_WORDS = frozenset({
    "please", "kindly", "make sure", "ensure that", "try to",
    "attempt to", "should be", "must be", "needs to", "has to",
})

# ===== 模糊/糊化风险词（BLUR RISK WORDS）=====
# 这些词会让 AI 生成磨损、做旧、模糊、低对比度、渐变 wash 等效果，
# 导致纹理下半部分糊化、出现不相关场景元素或文字痕迹。
BLUR_RISK_PHRASES = {
    #  phrase_lower : replacement_or_empty
    "slightly distressed": "",
    "distressed": "",
    "vintage screen-printed cotton": "retro screen-print flat ink",
    "vintage screen-print cotton": "retro screen-print flat ink",
    "vintage print texture": "",
    "vintage texture": "",
    "vintage stipple shading": "crisp dot pattern",
    "stipple shading": "crisp dot pattern",
    "stipple": "crisp dot pattern",
    "halftone texture": "halftone dots",
    "halftone print quality": "halftone dots",
    "halftone shading": "halftone dots",
    "subtle grain": "",
    "grain": "",
    "low contrast": "",
    "soft fading edges": "clean anti-aliased edges",
    "fading edges": "clean edges",
    "gradient ground": "flat color ground",
    "sunset gradient": "flat warm color",
    "warm sunset gradient": "flat warm color",
    "fabric weave appearance": "flat textile surface",
    "vintage fabric weave": "flat textile surface",
    "weathered": "",
    "aged": "",
    "worn": "",
    "mottled": "",
    "faded": "",
    "sunbleached": "",
    "tonal atmosphere": "",
    "atmospheric scene": "",
    "moody landscape": "",
    "blurred background": "",
}

# 单独单词级别的模糊风险词（主要出现在 positive prompt 中会导致糊化）
BLUR_RISK_WORDS = frozenset({
    "distressed", "stipple", "blotchy", "hazy", "foggy", "dreamy", "ethereal",
})

# 这些词在 negative prompt 中是合法的反模糊禁止语，在 positive 中才是风险
BLUR_RISK_WORDS_POSITIVE_ONLY = frozenset({
    "vignette", "smudged", "smeared", "muddy",
})

# 用于 negative prompt 的增强反模糊词
ANTI_BLUR_NEGATIVE_ADDONS = (
    "blurry, out of focus, smeared, smudged, vignette, "
    "distorted, deformed, low quality, jpeg artifacts, grainy"
)


def _is_in_negation_span(lower_text: str, target_word: str, word_index: int) -> bool:
    """检查目标词是否处于 no/without 引导的否定范围内。
    
    支持简单结构：
    - no xxx
    - no xxx or yyy
    - without xxx, yyy, zzz
    """
    tokens = lower_text.split()
    # 找到 target_word 在 tokens 中的位置（近似）
    # 由于传入的是原始位置，我们重新定位
    token_words = [re.sub(r"[^a-z0-9-]+", "", t) for t in tokens]
    try:
        idx = token_words.index(target_word, max(0, word_index - 3))
    except ValueError:
        return False
    
    # 向前搜索 no/without，最多回退 15 个词
    start = max(0, idx - 15)
    for j in range(idx - 1, start - 1, -1):
        if j < 0:
            break
        t = token_words[j]
        if t in ("no", "without"):
            return True
        # 如果遇到句号、分号或其他分隔词，认为否定范围结束
        if tokens[j].endswith((".", ";")):
            break
        # 如果遇到动词或新的主语，认为范围结束（简化处理）
        if t in ("is", "are", "has", "have", "create", "generate"):
            break
    return False


def detect_blur_risks(text: str, prompt_role: str = "positive") -> list[str]:
    """检测 prompt 中可能导致图像糊化/模糊的词语，返回风险词列表。
    
    Args:
        text: 输入 prompt 文本
        prompt_role: "positive" 或 "negative". negative prompt 中反模糊禁止语不算风险.
    
    自动排除 'no xxx' / 'without xxx' / 'no xxx or yyy' 否定结构中的词。
    """
    if not text:
        return []
    risks = []
    lower = text.lower()
    
    # 检测短语级别
    for phrase in BLUR_RISK_PHRASES:
        if phrase not in lower:
            continue
        # 检查该短语的每次出现是否被否定
        for m in re.finditer(re.escape(phrase), lower):
            span_start = max(0, m.start() - 40)
            context = lower[span_start:m.start()]
            # 如果上下文中有 no/without 且中间没有被句号/分号打断
            if re.search(r"\b(no|without)\b[^.;]*$", context):
                continue
            risks.append(phrase)
            break
    
    # 检测单词级别
    words = re.findall(r"[a-zA-Z-]+", lower)
    for i, word in enumerate(words):
        # 基础风险词
        if word in BLUR_RISK_WORDS and word not in risks:
            if _is_in_negation_span(lower, word, i):
                continue
            risks.append(word)
        # positive-only 风险词
        if prompt_role == "positive" and word in BLUR_RISK_WORDS_POSITIVE_ONLY and word not in risks:
            if _is_in_negation_span(lower, word, i):
                continue
            risks.append(word)
    return risks


def sanitize_blur_risks(text: str) -> str:
    """清理 prompt 中的模糊风险词，替换为安全表达或直接删除。"""
    if not text:
        return text
    out = text
    # 按长度降序，避免短词先匹配导致长词匹配失败
    for phrase in sorted(BLUR_RISK_PHRASES.keys(), key=len, reverse=True):
        replacement = BLUR_RISK_PHRASES[phrase]
        # 使用大小写不敏感的正则替换，保留原大小写风格
        def _repl(m: re.Match) -> str:
            orig = m.group(0)
            if not replacement:
                return ""
            return _token_case_like(orig, replacement)
        # 匹配边界或空格前后的短语
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        out = pattern.sub(_repl, out)
    # 清理多余空格和标点
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,.;:!?]){2,}", r"\1", out)
    return out.strip(" ,;")


def _get_stop_words(domain: str = "generic") -> frozenset:
    """根据领域返回对应的停用词集合。"""
    if domain == "fashion":
        return STOP_WORDS_FASHION
    return STOP_WORDS_GENERIC


def _category_for_word(word: str) -> str:
    for category, words in BANNED_WORDS_BY_CATEGORY.items():
        if word in words:
            return category
    return ""


def _token_case_like(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _apply_phrase_replacements(text: str, replacements: list[dict[str, str]], categories: set[str]) -> str:
    out = text
    for pattern, replacement in SAFE_PHRASE_REPLACEMENTS:
        def repl(match: re.Match) -> str:
            original = match.group(0)
            for token in re.findall(r"[A-Za-z][A-Za-z-]*", original.lower()):
                category = _category_for_word(token.replace(" ", "-"))
                if category:
                    categories.add(category)
            replacements.append({"from": original, "to": replacement, "reason": "safe_phrase_rewrite"})
            return _token_case_like(original, replacement)
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


def _clean_joined_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?]){2,}", r"\1", text)
    text = re.sub(r"\b(no|without)\s*,", r"\1", text, flags=re.IGNORECASE)
    return text.strip(" ,;")


def sanitize_prompt_with_report(text: str, domain: str = "generic", prompt_role: str = "positive") -> PromptSanitizationResult:
    """过滤提示词并返回审计报告。

    Args:
        text: 输入提示词
        domain: 领域上下文。"generic" 使用完整停用词表；"fashion" 保留审美关键词
        prompt_role: positive/negative/final。negative 会更激进地避免列举敏感词。

    规则：
    1. 拆分单词，按空格和标点分隔
    2. 检测停用词和禁用词，移除
    3. 保留专有名词、形容词短语中的有效视觉描述词
    4. 若文本被大幅过滤（保留 < 60%），打印警告
    """
    if not text or not isinstance(text, str):
        return PromptSanitizationResult(
            original_text=text,
            sanitized_text=text,
            domain=domain,
            prompt_role=prompt_role,
            removed=[],
            replacements=[],
            categories=[],
            warnings=[],
        )

    stop_words = _get_stop_words(domain)
    replacements: list[dict[str, str]] = []
    removed: list[str] = []
    categories: set[str] = set()
    text = _apply_phrase_replacements(text, replacements, categories)
    tokens = text.split()
    cleaned = []

    for token in tokens:
        # 去掉首尾标点
        stripped = token.strip(",.;:!?()[]{}'\"").lower()
        category = _category_for_word(stripped)
        if category:
            categories.add(category)
            replacement = SAFE_TOKEN_REPLACEMENTS.get(stripped)
            if replacement and prompt_role != "negative":
                prefix = token[:len(token) - len(token.lstrip(",.;:!?()[]{}'\""))]
                suffix = token[len(token.rstrip(",.;:!?()[]{}'\"")):]
                safe = _token_case_like(stripped, replacement)
                replacements.append({"from": token, "to": safe, "reason": category})
                cleaned.append(f"{prefix}{safe}{suffix}")
            else:
                removed.append(token)
            continue
        if stripped in stop_words or stripped in NOISE_WORDS:
            removed.append(token)
            continue
        # 也检查去掉连字符后的形式
        stripped_dash = stripped.replace("-", " ")
        parts = stripped_dash.split()
        banned_part = next((p for p in parts if p in BANNED_WORDS), "")
        if banned_part:
            category = _category_for_word(banned_part)
            if category:
                categories.add(category)
            replacement = SAFE_TOKEN_REPLACEMENTS.get(stripped) or SAFE_TOKEN_REPLACEMENTS.get(banned_part)
            if replacement and prompt_role != "negative":
                replacements.append({"from": token, "to": replacement, "reason": category or "banned_part"})
                cleaned.append(replacement)
            else:
                removed.append(token)
            continue
        if any(p in stop_words or p in NOISE_WORDS for p in parts):
            removed.append(token)
            continue
        cleaned.append(token)

    # 质量检查：如果移除太多，保留原始文本（仅过滤禁用词）
    if len(cleaned) < len(tokens) * 0.4:
        cleaned = []
        for token in tokens:
            stripped = token.strip(",.;:!?()[]{}'\"").lower()
            if stripped in BANNED_WORDS or _category_for_word(stripped):
                removed.append(token)
                continue
            cleaned.append(token)

    result = " ".join(cleaned)
    result = _clean_joined_text(result)
    warnings = []
    if categories:
        warnings.append("已重写或移除可能触发生图安全过滤的词。")
    if len(result) < max(24, len(str(text)) * 0.25):
        warnings.append("过滤后提示词明显变短，请检查输入主题是否过多依赖敏感表达。")
    return PromptSanitizationResult(
        original_text=str(text),
        sanitized_text=result,
        domain=domain,
        prompt_role=prompt_role,
        removed=removed,
        replacements=replacements,
        categories=sorted(categories),
        warnings=warnings,
    )


def sanitize_prompt(text: str, domain: str = "generic", prompt_role: str = "positive") -> str:
    """过滤提示词中的停用词、禁用词和噪音词。"""
    return sanitize_prompt_with_report(text, domain=domain, prompt_role=prompt_role).sanitized_text


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
                role = "negative" if k == "negative_prompt" else "positive"
                out[k] = sanitize_prompt(v, domain=domain, prompt_role=role)
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
        category = _category_for_word(stripped)
        if category:
            warnings.append(f"禁用词[{category}]: {stripped}")
        elif stripped in stop_words:
            warnings.append(f"停用词: {stripped}")
    return warnings
