"""LLM client abstraction.

A thin wrapper over the Anthropic / OpenAI SDKs. No actual API calls happen
until the user installs the SDK AND sets the relevant API key in env.

Both implementations are stubbed if the SDK isn't importable — calls raise a
clear error pointing the user at the install command and env-var name.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Literal

logger = logging.getLogger(__name__)

LLMProvider = Literal["anthropic", "openai", "stub"]


class LLMClient(ABC):
    """Common interface — returns the raw text response, no parsing."""

    provider: str
    model: str

    @abstractmethod
    def complete(self, prompt: str, max_tokens: int = 1500, temperature: float = 0.0) -> str:
        ...


class StubLLMClient(LLMClient):
    """Always raises. Used as the default until the user picks a provider."""

    provider = "stub"
    model = "stub"

    def complete(self, prompt: str, max_tokens: int = 1500, temperature: float = 0.0) -> str:
        raise NotImplementedError(
            "No LLM provider configured. Set LLM_PROVIDER=anthropic or openai in .env "
            "and add the corresponding ANTHROPIC_API_KEY / OPENAI_API_KEY."
        )


class AnthropicLLMClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str = "claude-3-5-sonnet-latest", api_key: str | None = None) -> None:
        self.model = model
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK is not installed. Run: pip install anthropic"
            ) from e
        self._key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self._key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in env or .env")
        self._client = Anthropic(api_key=self._key)

    def complete(self, prompt: str, max_tokens: int = 1500, temperature: float = 0.0) -> str:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if hasattr(block, "text"))


class OpenAILLMClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None) -> None:
        self.model = model
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "openai SDK is not installed. Run: pip install openai"
            ) from e
        self._key = api_key or os.getenv("OPENAI_API_KEY")
        if not self._key:
            raise RuntimeError("OPENAI_API_KEY is not set in env or .env")
        self._client = OpenAI(api_key=self._key)

    def complete(self, prompt: str, max_tokens: int = 1500, temperature: float = 0.0) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


def build_client(provider: LLMProvider, model: str | None = None) -> LLMClient:
    """Build a client per the configured provider. Defaults to StubLLMClient.

    Reads ANTHROPIC_API_KEY / OPENAI_API_KEY from env. Raises if the chosen
    provider's SDK isn't installed or key is missing.
    """
    if provider == "anthropic":
        return AnthropicLLMClient(model=model or "claude-3-5-sonnet-latest")
    if provider == "openai":
        return OpenAILLMClient(model=model or "gpt-4o-mini")
    return StubLLMClient()
