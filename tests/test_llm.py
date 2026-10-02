import json

import pytest
from pydantic import BaseModel

from app import llm
from app.llm import AuthError, LLMError, MockLLM


class Answer(BaseModel):
    title: str
    years: int


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_PAUSE", 0)


def mock(tmp_path, **fixture) -> MockLLM:
    (tmp_path / "a.json").write_text(json.dumps({"match": "технолог", **fixture}), "utf-8")
    return MockLLM("test", tmp_path)


def test_structured_output_is_validated_schema(tmp_path):
    model = mock(tmp_path, response={"title": "Технолог", "years": 7})
    answer = model.complete_structured(Answer, "system", "резюме: технолог, 7 лет")
    assert answer == Answer(title="Технолог", years=7)
    assert model.tokens_in > 0 and model.tokens_out > 0


def test_invalid_json_is_retried_once_with_error_text(tmp_path):
    model = mock(tmp_path, responses=['{"title": "Технолог", "years":', {"title": "Т", "years": 3}])
    assert model.complete_structured(Answer, "s", "технолог").years == 3
    assert len(model.calls) == 2
    assert "не прошёл проверку" in model.calls[1][1]


class Salary(BaseModel):
    amount: int | None = None


class Nested(BaseModel):
    title: str
    salary: Salary | None = None
    jobs: list[Salary] = []


@pytest.mark.parametrize(
    "salary,jobs",
    [
        ("None", "[]"),
        ("null", '[{"amount": None}, {"amount": 5}]'),
        ('{"amount": 7}', "[]"),
    ],
)
def test_nested_object_sent_as_string_is_repaired_without_new_request(tmp_path, salary, jobs):
    """Живой разбор через хаб: модель иногда присылает вложенный объект строкой."""
    (tmp_path / "a.json").write_text(
        json.dumps(
            {"match": "технолог", "response": {"title": "[ИМЯ]", "salary": salary, "jobs": jobs}}
        ),
        "utf-8",
    )
    model = MockLLM("test", tmp_path)
    answer = model.complete_structured(Nested, "s", "технолог")
    assert answer.title == "[ИМЯ]"  # строковое поле не трогаем, даже если похоже на список
    assert len(model.calls) == 1


def test_second_invalid_answer_fails(tmp_path):
    model = mock(tmp_path, responses=[{"title": "Т"}, {"years": "много"}])
    with pytest.raises(LLMError, match="не по форме"):
        model.complete_structured(Answer, "s", "технолог")
    assert len(model.calls) == 2


def test_network_error_retried_up_to_three_times(tmp_path):
    net = {"__error__": "network"}
    model = mock(tmp_path, responses=[net, net, net, {"title": "Т", "years": 1}])
    assert model.complete_structured(Answer, "s", "технолог").years == 1
    assert len(model.calls) == 4

    model = mock(tmp_path, responses=[net, net, net, net, {"title": "Т", "years": 1}])
    with pytest.raises(LLMError, match="не ответил"):
        model.complete_structured(Answer, "s", "технолог")


def test_overload_retried_then_refusal_is_human_text(tmp_path):
    busy = {"__error__": 529}
    model = mock(tmp_path, responses=[busy, busy, {"title": "Т", "years": 2}])
    assert model.complete_structured(Answer, "s", "технолог").years == 2
    model = mock(tmp_path, responses=[{"__error__": 400}, {"title": "Т", "years": 2}])
    with pytest.raises(LLMError, match="отклонил запрос \\(код 400\\)"):
        model.complete_structured(Answer, "s", "технолог")
    assert len(model.calls) == 1  # отказ не повторяется


@pytest.mark.parametrize("code,retried", [(529, True), (503, True), (500, True), (404, False)])
def test_sdk_status_errors_classified(monkeypatch, code, retried):
    """Настоящие классы SDK: 529 (OverloadedError) — не InternalServerError, но повторяется."""
    import anthropic
    import httpx

    model = llm.AnthropicLLM("m", "http://hub.local", "k")
    response = httpx.Response(code, request=httpx.Request("POST", "http://hub.local"))
    calls = []

    def fail(*_):
        calls.append(1)
        raise model.client._make_status_error("сбой", body=None, response=response)

    monkeypatch.setattr(model, "_call", fail)
    with pytest.raises(LLMError) as err:
        model.complete_structured(Answer, "s", "технолог")
    assert len(calls) == (llm.NETWORK_RETRIES + 1 if retried else 1)
    assert "Error" not in str(err.value)  # русский текст, а не имя исключения
    if code == 529:
        assert isinstance(
            model.client._make_status_error("x", body=None, response=response),
            anthropic.OverloadedError,
        )


def test_cached_input_counted_in_volume(monkeypatch):
    """Живая проверка этапа 3: хаб отдаёт часть входа как кэш — он тоже в счётчике."""
    from types import SimpleNamespace

    model = llm.AnthropicLLM("m", "http://hub.local", "k")
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=900,
        cache_creation_input_tokens=None,
    )  # fmt: skip
    block = SimpleNamespace(type="tool_use", input={"title": "Т", "years": 1})
    reply = SimpleNamespace(usage=usage, content=[block])
    monkeypatch.setattr(model.client.messages, "create", lambda **kw: reply)
    assert model.complete_structured(Answer, "s", "технолог").years == 1
    assert (model.tokens_in, model.tokens_out) == (1000, 50)


def test_bad_key_gives_human_message(tmp_path):
    model = mock(tmp_path, response={"__error__": "auth"})
    with pytest.raises(AuthError, match="Ключ не принят"):
        model.complete_structured(Answer, "s", "технолог")


def test_empty_balance_named_plainly_and_not_retried(tmp_path):
    """Живой прогон 0.4: хаб ответил 402 «Insufficient balance». Дело не в адресе и не в
    сети, а в балансе ключа, и каждый следующий запрос откажет так же."""
    model = mock(tmp_path, response={"__error__": 402})
    with pytest.raises(AuthError, match="на его балансе не хватает денег"):
        model.complete_structured(Answer, "s", "технолог")
    assert len(model.calls) == 1


def test_provider_chosen_by_settings(session, monkeypatch):
    from app import config

    config.save(
        {"llm_provider": "anthropic", "llm_base_url": "http://hub.local", "llm_api_key": "k"}
    )
    assert isinstance(llm.get_llm("parse"), llm.AnthropicLLM)
    assert llm.get_llm("parse").model == "claude-haiku-4.5"
    assert llm.get_llm("eval").model == "claude-sonnet-5"
    config.save({"llm_provider": "openai"})
    assert isinstance(llm.get_llm(), llm.OpenAILLM)
    config.save({"llm_provider": "mock"})
    assert isinstance(llm.get_llm(), llm.MockLLM)


def test_env_is_fallback_for_settings(session, monkeypatch):
    from app import config

    monkeypatch.setenv("LLM_MODEL_PARSE", "из-окружения")
    assert config.get("llm_model_parse") == "из-окружения"
    config.save({"llm_model_parse": "из-настроек"})
    assert config.get("llm_model_parse") == "из-настроек"
