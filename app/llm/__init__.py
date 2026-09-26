"""Адаптер ИИ-модели: complete_structured(schema, system, user) → заполненная Pydantic-схема.

Структурный вывод — через принудительный вызов инструмента: модель обязана вернуть
аргументы инструмента `answer`, их JSON-схема — схема ответа. Так работают и
Anthropic Messages API (в том числе прокси ClaudeHub), и OpenAI-совместимые серверы.
Библиотеку instructor не берём: повтор с указанием на ошибку и учёт объёма текста
укладываются в тонкий слой ниже, а мок идёт тем же путём проверки, что и живые модели.

Временный сбой — сеть, тайм-аут, 429, 5xx и перегрузка 529 — повторяется до трёх раз с
паузой; прочие отказы сервиса (4xx) сразу становятся `LLMError` с понятным текстом:
запись уходит в «не удалось», задача идёт дальше. Ответ не по схеме повторяется один
раз, с текстом ошибки в запросе.
"""

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app import config

NETWORK_RETRIES = 3
RETRY_PAUSE = 2.0  # секунды; растёт с каждой попыткой
TOOL = "answer"


class LLMError(Exception):
    """Ошибка с понятным пользователю текстом."""


class AuthError(LLMError):
    pass


AUTH_MESSAGE = "Ключ доступа не подошёл. Проверьте, что скопировали его целиком."
BUSY_MESSAGE = "сервис ИИ не ответил"


class LLM:
    network_errors: tuple = ()  # временные: повторяем
    auth_errors: tuple = ()
    status_errors: tuple = ()  # прочие ответы с кодом: 5xx повторяем, остальное — отказ

    def __init__(self, model: str):
        self.model = model
        self.tokens_in = 0
        self.tokens_out = 0
        self._lock = threading.Lock()

    def complete_structured(self, schema: type[BaseModel], system: str, user: str) -> BaseModel:
        prompt = user
        for attempt in (1, 2):
            raw = self._send(schema, system, prompt)
            try:
                return schema.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    raise LLMError("ответ модели не по форме") from exc
                prompt = (
                    f"{user}\n\nПрошлый ответ не прошёл проверку: {str(exc)[:500]}\n"
                    "Верни ответ строго по схеме."
                )
        raise AssertionError("недостижимо")

    def _send(self, schema, system, user):
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                return self._call(schema, system, user)
            except self.auth_errors as exc:
                raise AuthError(AUTH_MESSAGE) from exc
            except (*self.network_errors, *self.status_errors) as exc:
                code = getattr(exc, "status_code", None)
                if isinstance(exc, self.status_errors) and not isinstance(exc, self.network_errors):
                    if code is None or code < 500:
                        raise LLMError(
                            f"сервис ИИ отклонил запрос (код {code}); "
                            "проверьте модель и адрес в «Настройках»"
                        ) from exc
                if attempt == NETWORK_RETRIES:
                    raise LLMError(BUSY_MESSAGE) from exc
                time.sleep(RETRY_PAUSE * (attempt + 1))
        raise AssertionError("недостижимо")

    def _count(self, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out

    def _call(self, schema: type[BaseModel], system: str, user: str) -> dict | str:
        raise NotImplementedError


class AnthropicLLM(LLM):
    def __init__(self, model: str, base_url: str, api_key: str):
        import anthropic

        super().__init__(model)
        self.client = anthropic.Anthropic(
            base_url=base_url or None, api_key=api_key, max_retries=0, timeout=120
        )
        # OverloadedError (529) в SDK наследует APIStatusError, а не InternalServerError.
        self.network_errors = (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.OverloadedError,
        )
        self.auth_errors = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
        self.status_errors = (anthropic.APIStatusError,)

    def _call(self, schema, system, user):
        reply = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": TOOL,
                    "description": "Ответ строго по схеме",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": TOOL},
        )
        self._count(reply.usage.input_tokens, reply.usage.output_tokens)
        for block in reply.content:
            if block.type == "tool_use":
                return block.input
        return "".join(getattr(b, "text", "") for b in reply.content)


class OpenAILLM(LLM):
    def __init__(self, model: str, base_url: str, api_key: str):
        import openai

        super().__init__(model)
        self.client = openai.OpenAI(
            base_url=base_url or None, api_key=api_key, max_retries=0, timeout=120
        )
        self.network_errors = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )
        self.auth_errors = (openai.AuthenticationError, openai.PermissionDeniedError)
        self.status_errors = (openai.APIStatusError,)  # 5xx любого вида — повтор

    def _call(self, schema, system, user):
        reply = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": TOOL,
                        "description": "Ответ строго по схеме",
                        "parameters": schema.model_json_schema(),
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": TOOL}},
        )
        if reply.usage:
            self._count(reply.usage.prompt_tokens, reply.usage.completion_tokens)
        message = reply.choices[0].message
        if message.tool_calls:
            return message.tool_calls[0].function.arguments
        return message.content or ""


class MockStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"код {status_code} (мок)")
        self.status_code = status_code


class MockLLM(LLM):
    """Ответы из фикстур `*.json`: {"match": "подстрока запроса", "response": {...}}
    или список таких объектов в одном файле.

    Поле "schema" (имя Pydantic-схемы) ограничивает ответ запросами этой схемы.
    Вместо "response" можно дать "responses": [...] — ответы по очереди на повторные
    вызовы; строка — «сырой» ответ (например, битый JSON), {"__error__": "network"} —
    сетевой сбой, {"__error__": 529} — ответ сервиса с этим кодом. Объём текста считается
    как символы / 3.

    Номера строк в записанном ответе можно задать фрагментом: {"__lines__": "текст"}
    превращается в [n, n] по строке пронумерованного запроса «n| …», где он стоит,
    {"__lines__": ["от", "до"]} — в [n, m]; не нашёлся — null. Так ответ не зависит от
    того, как импорт разложил резюме по строкам.
    """

    network_errors = (ConnectionError,)
    auth_errors = (PermissionError,)
    status_errors = (MockStatusError,)

    def __init__(self, model: str, fixtures: str | Path):
        super().__init__(model)
        self.fixtures = []
        for path in sorted(Path(fixtures).glob("*.json")):
            data = json.loads(path.read_text("utf-8"))
            self.fixtures += data if isinstance(data, list) else [data]
        self.served: Counter = Counter()
        self.calls: list[tuple[str, str]] = []

    def _call(self, schema, system, user):
        with self._lock:
            self.calls.append((system, user))
            # Ответ под схему запроса важнее общего: у оценки и разбора одного резюме
            # совпадает текст, различает их поле "schema" в фикстуре.
            found = [
                i
                for i, f in enumerate(self.fixtures)
                if f["match"] in user and f.get("schema", schema.__name__) == schema.__name__
            ]
            found.sort(key=lambda i: "schema" not in self.fixtures[i])
            index = found[0] if found else None
            if index is None:
                return "{}"
            fixture = self.fixtures[index]
            answers = fixture.get("responses") or [fixture["response"]]
            answer = answers[min(self.served[index], len(answers) - 1)]
            self.served[index] += 1
        if isinstance(answer, dict) and answer.get("__error__") == "network":
            raise ConnectionError("сбой сети (мок)")
        if isinstance(answer, dict) and answer.get("__error__") == "auth":
            raise PermissionError("ключ (мок)")
        if isinstance(answer, dict) and isinstance(answer.get("__error__"), int):
            raise MockStatusError(answer["__error__"])
        self._count(
            (len(system) + len(user)) // 3, len(json.dumps(answer, ensure_ascii=False)) // 3
        )
        return _resolve_lines(answer, user)


def _line_of(fragment: str, user: str) -> int | None:
    needle = " ".join(fragment.lower().split())
    for match in re.finditer(r"^(\d+)\| (.*)$", user, re.MULTILINE):
        if needle in " ".join(match.group(2).lower().split()):
            return int(match.group(1))
    return None


def _resolve_lines(answer, user: str):
    if isinstance(answer, list):
        return [_resolve_lines(a, user) for a in answer]
    if not isinstance(answer, dict):
        return answer
    if "__lines__" in answer:
        where = answer["__lines__"]
        start, stop = (where, where) if isinstance(where, str) else where
        found = _line_of(start, user), _line_of(stop, user)
        return list(found) if None not in found else None
    return {k: _resolve_lines(v, user) for k, v in answer.items()}


def get_llm(purpose: str = "parse") -> LLM:
    """Модель для разбора резюме (parse) или оценки (eval) по текущим настройкам."""
    model = config.get(f"llm_model_{purpose}")
    provider = config.get("llm_provider")
    if provider == "mock":
        return MockLLM(model, config.get("llm_fixtures"))
    base_url, key = config.get("llm_base_url"), config.get("llm_api_key")
    if provider == "openai":
        return OpenAILLM(model, base_url, key)
    return AnthropicLLM(model, base_url, key)
