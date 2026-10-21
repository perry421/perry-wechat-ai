"""回复正文净化。

模型偶尔会把提示词里的祈使句复述进正文。实测 DeepSeek 在自动回复模式下
返回过 ``只输出正文。转不了，你打字吧``——``只输出正文。`` 显然不是 Perry
会说的话，是提示词的"输出要求"被当成了正文的一部分。

这里做一层**保守**清洗：只剥掉明确是指令残片的固定前缀，其余内容一律
不动。真人回复的语气很脆弱，改写多了就假了。
"""

from __future__ import annotations

import re

# 模型复述提示词时留下的固定前缀。按出现顺序逐个尝试剥离。
_PREFIX_PATTERNS = (
    r"^只输出(?:回复)?(?:正文|内容)[：:，,。]?\s*",
    r"^只(?:输出|回复)(?:正文|内容)[：:]\s*",
    r"^(?:回复|回答|正文|输出|内容|最终回复|回复正文)\s*[：:]\s*",
    r"^(?:好的|ok|OK|Okay)[，,。:：]\s*(?=[一-鿿])",
)

# 模型偶尔用引号包住整段。
_QUOTE_WRAPPERS = (("“", "”"), ('"', '"'), ("「", "」"), ("『", "』"))

# 常见的"解释性尾巴"，出现在正文末尾。
_TAIL_PATTERNS = (
    r"\s*（?如需[^）]*）?\s*$",
    r"\s*\(?(?:以上|回复如上)[^)）]*\)?\s*$",
)


def clean_reply(text: str) -> str:
    """清洗模型输出，返回可直接发送的正文。

    只做确定性的剥离，不做同义改写——清洗过度会把真人语气洗没了。
    """
    if not text:
        return ""

    cleaned = text.strip()
    # 去掉 markdown 包裹
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()

    changed = True
    while changed:
        changed = False
        for pattern in _PREFIX_PATTERNS:
            new = re.sub(pattern, "", cleaned, count=1)
            if new != cleaned:
                cleaned, changed = new.strip(), True
        for left, right in _QUOTE_WRAPPERS:
            if (
                len(cleaned) > len(left) + len(right)
                and cleaned.startswith(left)
                and cleaned.endswith(right)
            ):
                cleaned = cleaned[len(left) : -len(right)].strip()
                changed = True

    for pattern in _TAIL_PATTERNS:
        new = re.sub(pattern, "", cleaned, count=1)
        if new != cleaned:
            cleaned, changed = new.strip(), True

    return cleaned.strip()
