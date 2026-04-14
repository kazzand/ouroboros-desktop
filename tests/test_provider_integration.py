"""Integration tests — real API calls to each supported LLM provider.

These tests call live provider APIs with minimal prompts to verify
that the full routing+request+response path works end-to-end.

Requirements:
  - Corresponding API key must be set as an env variable.
  - Tests are skipped automatically when the key is absent.
  - Marked with @pytest.mark.integration — excluded by default via
    pyproject.toml addopts ("-m 'not integration'"). Only the
    dedicated CI integration-test job overrides this with "-m integration".

Cost: ~$0.006 total for all three providers per run.
"""

import os
import pytest

from ouroboros.llm import LLMClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_PROMPT = [{"role": "user", "content": "Respond with exactly one word: OK"}]
LIGHT_MAX_TOKENS = 32


def _assert_basic_response(response_msg: dict, usage: dict, expected_provider: str):
    """Shared assertions that every provider must satisfy."""
    # Response must contain non-empty text
    content = response_msg.get("content") or ""
    assert len(content.strip()) > 0, "Empty response content"

    # Usage must report token counts
    assert usage.get("prompt_tokens", 0) > 0, "Missing prompt_tokens in usage"
    assert usage.get("completion_tokens", 0) > 0, "Missing completion_tokens in usage"

    # Model field should be present
    assert usage.get("model") or usage.get("resolved_model"), "Missing model in usage"

    # Provider must match the expected routing target
    actual_provider = usage.get("provider", "")
    assert actual_provider == expected_provider, (
        f"Provider routing mismatch: expected '{expected_provider}', "
        f"got '{actual_provider}'"
    )


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)
def test_openrouter_basic_chat():
    """Verify OpenRouter routing with a cheap model."""
    client = LLMClient()
    model = "anthropic/claude-sonnet-4.6"  # cheap, fast
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model=model,
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "openrouter")


# ---------------------------------------------------------------------------
# OpenAI (direct)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
def test_openai_direct_basic_chat():
    """Verify direct OpenAI routing."""
    client = LLMClient()
    model = "openai::gpt-4.1-mini"  # cheapest official model
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model=model,
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "openai")


# ---------------------------------------------------------------------------
# Anthropic (direct)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_anthropic_direct_basic_chat():
    """Verify direct Anthropic routing."""
    client = LLMClient()
    model = "anthropic::claude-sonnet-4.6"  # cheapest Anthropic model
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model=model,
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "anthropic")


# ---------------------------------------------------------------------------
# Cross-provider: each key alone is enough for basic functionality
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)
def test_openrouter_only_mode(monkeypatch):
    """With ONLY OpenRouter key, basic chat must work."""
    # Clear other keys so we're truly OpenRouter-only
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = LLMClient()
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model="anthropic/claude-sonnet-4.6",
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "openrouter")


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
def test_openai_only_mode(monkeypatch):
    """With ONLY OpenAI key, direct OpenAI routing must work."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = LLMClient()
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model="openai::gpt-4.1-mini",
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "openai")


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_anthropic_only_mode(monkeypatch):
    """With ONLY Anthropic key, direct Anthropic routing must work."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = LLMClient()
    resp, usage = client.chat(
        messages=SIMPLE_PROMPT,
        model="anthropic::claude-sonnet-4.6",
        max_tokens=LIGHT_MAX_TOKENS,
        reasoning_effort="low",
    )
    _assert_basic_response(resp, usage, "anthropic")
