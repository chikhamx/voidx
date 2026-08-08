"""Google Gemini (native API via langchain-google-genai)."""

from __future__ import annotations

import subprocess
import sys

from voidx.llm.domain.model import ModelConfig
from voidx.llm.providers.base import ProviderSpec
from voidx.llm.domain.model import ReasoningEffort
from voidx.llm.providers.common import GEMINI_THINKING_BUDGETS, map_effort, resolve_effort

GEMINI_API_VERSION = "v1beta"


def strip_gemini_version_suffix(url: str) -> str:
    """Strip a trailing /v1beta from a Gemini base_url.

    google-genai SDK appends the api version (v1beta) to every request URL,
    so a user-supplied base_url that already ends with /v1beta produces
    /v1beta/v1beta/... and a 404.
    """
    normalized = url.strip().rstrip("/")
    suffix = f"/{GEMINI_API_VERSION}"
    if normalized.endswith(suffix):
        return normalized[: -len(suffix)]
    return normalized


_GEMINI3_PREFIXES = (
    "gemini-3",
    "gemini-4",
)

# ChatGoogleGenerativeAI.thinking_level only accepts these values.
_GEMINI_THINKING_LEVELS = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
)


def _normalize_gemini_model_name(model: str) -> str:
    name = (model or "").lower().strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def _is_gemini3_plus(model: str) -> bool:
    """Whether a Gemini model uses thinking_level (3+) vs thinking_budget (2.5)."""
    name = _normalize_gemini_model_name(model)
    return any(name.startswith(p) for p in _GEMINI3_PREFIXES)


def gemini_reasoning(config: ModelConfig) -> dict:
    effort = resolve_effort(config)
    if effort is ReasoningEffort.NONE:
        return {}
    kwargs: dict = {"include_thoughts": True}
    if _is_gemini3_plus(config.model):
        # Custom providers may not hit the gemini model table, so clamp here.
        level = map_effort(effort, _GEMINI_THINKING_LEVELS)
        kwargs["thinking_level"] = level.value
    else:
        kwargs["thinking_budget"] = GEMINI_THINKING_BUDGETS.get(effort, 8_192)
    return kwargs


def ensure_gemini_dep() -> None:
    """Ensure langchain-google-genai is importable; auto-install if missing.

    Tries to import the package. On ImportError, silently runs
    ``pip install langchain-google-genai`` up to 3 times, retrying the import
    after each install. Raises ImportError with a manual install hint only
    after all retries are exhausted.
    """
    try:
        import langchain_google_genai  # noqa: F401
        return
    except ImportError:
        pass

    last_err = ""
    for _ in range(3):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "langchain-google-genai>=4.0.0"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            last_err = f"pip install timed out after 120s: {e}"
            continue
        if result.returncode != 0:
            last_err = (result.stderr or result.stdout or "").strip()[-200:]
            continue
        try:
            import langchain_google_genai  # noqa: F401
            return
        except ImportError as e:
            last_err = str(e)
            continue

    raise ImportError(
        "langchain-google-genai is required for Gemini protocol. "
        "Auto-install failed"
        + (f": {last_err}" if last_err else "")
        + ". Install manually with: pip install voidx[gemini]"
    )


SPEC = ProviderSpec(
    name="gemini",
    protocol="gemini",
    context_limit=1_000_000,
    static_models=(
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ),
    reasoning=gemini_reasoning,
)
