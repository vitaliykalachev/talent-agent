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


def test_bad_key_gives_human_message(tmp_path):
    model = mock(tmp_path, response={"__error__": "auth"})
    with pytest.raises(AuthError, match="Ключ доступа не подошёл"):
        model.complete_structured(Answer, "s", "технолог")


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
