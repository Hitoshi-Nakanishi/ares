import pytest


@pytest.fixture(autouse=True)
def isolate_operator_profile(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_CONFIG", raising=False)
