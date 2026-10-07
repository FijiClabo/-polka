"""Слой-адаптер LLMProvider: основной Claude (Anthropic API), запасной YandexGPT.

Названия моделей и ключи — только из переменных окружения.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from settings import Settings, get_settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Провайдер недоступен или вернул ошибку — можно пробовать следующий."""


class LLMUnavailable(Exception):
    """Ни один провайдер не ответил."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str


class LLMProvider(Protocol):
    name: str

    async def complete(
        self, system: str, context: str, prompt: str, *, schema: dict | None, cheap: bool, max_tokens: int
    ) -> LLMResult: ...


# --------------------------------------------------------------------------- Anthropic


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, s: Settings):
        import anthropic

        self._anthropic = anthropic
        kwargs: dict = {"api_key": s.anthropic_api_key, "timeout": s.llm_timeout_sec, "max_retries": 1}
        if s.anthropic_base_url:
            kwargs["base_url"] = s.anthropic_base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)
        self.model = s.anthropic_model
        self.model_cheap = s.anthropic_model_cheap
        self.effort = s.anthropic_effort
        self.refusal_fallback = s.anthropic_refusal_fallback

    @staticmethod
    def _supports_effort(model: str) -> bool:
        return "haiku" not in model

    @staticmethod
    def _supports_server_fallback(model: str) -> bool:
        return any(x in model for x in ("opus-5", "sonnet-5-5", "fable-5"))

    async def complete(
        self, system: str, context: str, prompt: str, *, schema: dict | None, cheap: bool, max_tokens: int
    ) -> LLMResult:
        a = self._anthropic
        model = self.model_cheap if cheap else self.model
        content = []
        if context:
            # контекст (книга, текст отрезка) стабилен между уточнениями — кэшируем префикс
            content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": prompt})
        output_config: dict = {}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if self.effort and self._supports_effort(model):
            output_config["effort"] = self.effort
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        if output_config:
            params["output_config"] = output_config
        try:
            if self.refusal_fallback and self._supports_server_fallback(model):
                try:
                    resp = await self.client.beta.messages.create(
                        **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                    )
                except a.BadRequestError as e:
                    log.warning("anthropic fallback param rejected, retry without: %s", e.message)
                    self.refusal_fallback = False
                    resp = await self.client.messages.create(**params)
            else:
                resp = await self.client.messages.create(**params)
        except a.APIStatusError as e:
            raise LLMError(f"anthropic {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"anthropic connection: {e}") from e
        except a.APIError as e:
            raise LLMError(f"anthropic: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("anthropic refusal")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError(f"anthropic empty response, stop={resp.stop_reason}")
        return LLMResult(text=text, provider=self.name, model=model)


# --------------------------------------------------------------------------- YandexGPT


class YandexGPTProvider:
    name = "yandex"
    URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self, s: Settings):
        self.key = s.yandex_api_key
        self.folder = s.yandex_folder_id
        self.model = s.yandexgpt_model
        self.model_cheap = s.yandexgpt_model_cheap
        self.timeout = s.llm_timeout_sec

    async def complete(
        self, system: str, context: str, prompt: str, *, schema: dict | None, cheap: bool, max_tokens: int
    ) -> LLMResult:
        model = self.model_cheap if cheap else self.model
        sys_text = system
        if schema:
            sys_text += "\n\nОтвет — только JSON-объект по схеме:\n" + json.dumps(schema, ensure_ascii=False)
        body: dict = {
            "modelUri": f"gpt://{self.folder}/{model}",
            "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": str(min(max_tokens, 4000))},
            "messages": [
                {"role": "system", "text": sys_text},
                {"role": "user", "text": f"{context}\n\n{prompt}" if context else prompt},
            ],
        }
        if schema:
            body["jsonObject"] = True
        headers = {"Authorization": f"Api-Key {self.key}", "x-folder-id": self.folder}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.URL, json=body, headers=headers)
                if r.status_code == 400 and schema:
                    body.pop("jsonObject", None)
                    r = await c.post(self.URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"yandex connection: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"yandex {r.status_code}: {r.text[:300]}")
        try:
            alt = r.json()["result"]["alternatives"][0]
            text = alt["message"]["text"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"yandex bad response: {r.text[:300]}") from e
        if alt.get("status") in ("ALTERNATIVE_STATUS_CONTENT_FILTER",):
            raise LLMError("yandex content filter")
        return LLMResult(text=text, provider=self.name, model=model)


# --------------------------------------------------------------------------- демо (без ключей)


class DemoProvider:
    """Заглушка для локального просмотра без ключей: засчитывает любой пересказ. LLM_PROVIDERS=demo."""

    name = "demo"
    model = "demo"

    async def complete(
        self, system: str, context: str, prompt: str, *, schema: dict | None, cheap: bool, max_tokens: int
    ) -> LLMResult:
        if schema and "summary" in schema.get("properties", {}):
            return LLMResult(json.dumps({"summary": "Краткое содержание (демо).",
                                         "retell_prompt": "Что запомнилось в этом отрезке? Расскажи своими словами."},
                                        ensure_ascii=False), self.name, self.model)
        if schema and "intro" in schema.get("properties", {}):
            return LLMResult('{"intro": "Итог книги (демо)."}', self.name, self.model)
        return LLMResult(json.dumps({
            "verdict": "accepted", "confidence": 0.9,
            "reply": "Засчитываю — видно, что отрезок прочитан. (Это демо-режим без настоящего ИИ.)",
            "question": "Какой момент показался самым неожиданным?", "note_for_summary": "",
        }, ensure_ascii=False), self.name, self.model)


# --------------------------------------------------------------------------- цепочка


class LLMChain:
    """Пробует провайдеров по порядку. Ошибка одного → следующий."""

    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers

    @property
    def available(self) -> bool:
        return bool(self.providers)

    async def complete(
        self, system: str, context: str, prompt: str, *, schema: dict | None = None, cheap: bool = False,
        max_tokens: int = 4000, start_from: int = 0,
    ) -> LLMResult:
        errors = []
        for p in self.providers[start_from:]:
            try:
                return await p.complete(system, context, prompt, schema=schema, cheap=cheap, max_tokens=max_tokens)
            except LLMError as e:
                log.warning("LLM provider %s failed: %s", p.name, e)
                errors.append(f"{p.name}: {e}")
        raise LLMUnavailable("; ".join(errors) or "no providers configured")


_chain: LLMChain | None = None


def build_chain(s: Settings | None = None) -> LLMChain:
    s = s or get_settings()
    providers: list[LLMProvider] = []
    for name in s.llm_order:
        if name == "anthropic" and s.anthropic_api_key:
            providers.append(AnthropicProvider(s))
        elif name == "yandex" and s.yandex_api_key and s.yandex_folder_id:
            providers.append(YandexGPTProvider(s))
        elif name == "demo":
            providers.append(DemoProvider())
    return LLMChain(providers)


def get_llm() -> LLMChain:
    global _chain
    if _chain is None:
        _chain = build_chain()
    return _chain


def set_llm(chain: LLMChain | None) -> None:
    """Подмена в тестах."""
    global _chain
    _chain = chain
