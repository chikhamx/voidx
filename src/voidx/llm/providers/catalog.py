"""Canonical ordered catalog of built-in LLM providers."""

from voidx.llm.providers.anthropic import SPEC as ANTHROPIC_SPEC
from voidx.llm.providers.deepseek import SPEC as DEEPSEEK_SPEC
from voidx.llm.providers.doubao import SPEC as DOUBAO_SPEC
from voidx.llm.providers.gemini import SPEC as GEMINI_SPEC
from voidx.llm.providers.kimi import SPEC as KIMI_SPEC
from voidx.llm.providers.longcat import SPEC as LONGCAT_SPEC
from voidx.llm.providers.mimo import SPECS as MIMO_SPECS
from voidx.llm.providers.minimax import SPEC as MINIMAX_SPEC
from voidx.llm.providers.openai import SPEC as OPENAI_SPEC
from voidx.llm.providers.openrouter import SPEC as OPENROUTER_SPEC
from voidx.llm.providers.qwen import SPEC as QWEN_SPEC
from voidx.llm.providers.typex import SPEC as TYPEX_SPEC
from voidx.llm.providers.xunfei import SPEC as XUNFEI_SPEC
from voidx.llm.providers.zhipu import SPEC as ZHIPU_SPEC

PROVIDER_SPECS = (
    ANTHROPIC_SPEC,
    DEEPSEEK_SPEC,
    DOUBAO_SPEC,
    GEMINI_SPEC,
    KIMI_SPEC,
    LONGCAT_SPEC,
    *MIMO_SPECS,
    MINIMAX_SPEC,
    OPENAI_SPEC,
    OPENROUTER_SPEC,
    QWEN_SPEC,
    TYPEX_SPEC,
    XUNFEI_SPEC,
    ZHIPU_SPEC,
)
