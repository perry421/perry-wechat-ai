"""按任务难度路由到不同模型，并处理失败降级。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ModelEndpoint
from .models import ChatContext, Difficulty, LLMReply, ProviderError
from .providers.openai_compat import Provider

_TRIVIAL_PATTERNS = (
    r"^(你好|您好|hi|hello|hey|在吗|在不在|早|晚安|早上好|中午好|下午好|晚上好)[!！。~？?]*$",
    r"^(好的|好|行|嗯|收到|谢谢|多谢|辛苦了|ok|OK|okay|👍|😊|哈哈+)$",
    r"^(早上好|晚安|生日快乐|新年快乐|节日快乐)[!！。]*$",
)

_COMPLEX_HINTS = (
    "为什么",
    "怎么理解",
    "如何",
    "分析",
    "比较",
    "方案",
    "设计",
    "帮我写",
    "润色",
    "翻译",
    "总结",
    "规划",
    "步骤",
    "评估",
    "区别",
    "原理",
    "架构",
)


@dataclass(frozen=True)
class RouteDecision:
    difficulty: Difficulty
    endpoint_name: str
    reason: str


def classify(context: ChatContext) -> Difficulty:
    """判断任务难度。规则优先，简单问题走低开销模型。"""
    text = context.trigger.text.strip()
    if not text:
        return Difficulty.SIMPLE

    # 群聊里别人说的话通常不是在对助手提问
    if context.chat.is_group and not context.trigger.is_from_me:
        if len(text) > 60:
            return Difficulty.COMPLEX
        return Difficulty.SIMPLE

    lowered = text.lower()
    for pattern in _TRIVIAL_PATTERNS:
        if re.match(pattern, lowered):
            return Difficulty.TRIVIAL

    if len(text) > 120:
        return Difficulty.COMPLEX
    if any(hint in text for hint in _COMPLEX_HINTS):
        return Difficulty.COMPLEX
    return Difficulty.SIMPLE


class Router:
    """把上下文送到合适的供应商，失败时按顺序降级。"""

    def __init__(self, providers: dict[str, Provider], prices: dict[str, ModelEndpoint]):
        self.providers = providers
        self.prices = prices

    def _chain(self, difficulty: Difficulty) -> list[str]:
        """按难度给出尝试顺序。

        - 简单/琐碎：优先本地模型（零成本），再退到主模型。
        - 复杂：直接用主模型，本地模型作为最后的保底。
        """
        primary = "primary"
        fallback = "fallback"
        local = "local"

        def has(name: str) -> bool:
            prov = self.providers.get(name)
            return prov is not None and (
                prov.endpoint.configured if hasattr(prov, "endpoint") else True
            )

        if difficulty is Difficulty.TRIVIAL:
            order = [local, primary, fallback]
        elif difficulty is Difficulty.SIMPLE:
            order = [primary, local, fallback]
        else:
            order = [primary, fallback, local]
        return [name for name in order if has(name)]

    def decide(self, context: ChatContext) -> RouteDecision:
        difficulty = classify(context)
        chain = self._chain(difficulty)
        if not chain:
            return RouteDecision(difficulty, "none", "没有可用端点")
        return RouteDecision(difficulty, chain[0], f"{difficulty.value} → {chain[0]}")

    def generate(
        self, system_prompt: str, context: ChatContext, *, temperature: float = 0.6
    ) -> LLMReply:
        """生成回复。全部端点失败时抛出最后一个 ProviderError。"""
        decision = self.decide(context)
        chain = self._chain(decision.difficulty)
        if not chain:
            raise ProviderError("没有可用的模型端点，请检查 .env 配置", kind="unavailable")

        last_error: ProviderError | None = None
        for index, name in enumerate(chain):
            provider = self.providers.get(name)
            if provider is None:
                continue
            try:
                reply = provider.complete(system_prompt, context, temperature=temperature)
            except ProviderError as exc:
                last_error = exc
                if not exc.can_degrade:
                    raise
                continue
            if index > 0:
                reply.degraded = True
            return reply

        raise last_error or ProviderError("所有模型端点均失败", kind="unavailable")
