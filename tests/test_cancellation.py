from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from flowchart_agent.cancellation import OperationCancelled, run_cancellable_process
from flowchart_agent.config import ModelConfig
from flowchart_agent.llm import client as client_module
from flowchart_agent.llm.client import LLMClient, _collect_stream


def _text_chunk(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text, reasoning_content=None, tool_calls=None,
        ))]
    )


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890"])
def test_llm_client_ignores_system_proxy_and_only_uses_configured_proxy(
    monkeypatch, proxy
):
    captured = {}

    class FakeHttpClient:
        def __init__(self, **kwargs):
            captured["http"] = kwargs

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["openai"] = kwargs

    monkeypatch.setattr(client_module, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(client_module, "OpenAI", FakeOpenAI)

    llm = LLMClient(ModelConfig(
        name="test", api_key="test", base_url="http://model.local/v1", proxy=proxy
    ))
    llm._get_client()

    assert captured["http"] == {"proxy": proxy, "trust_env": False}
    assert captured["openai"]["http_client"].__class__ is FakeHttpClient


def test_stream_collection_honours_cancel_between_chunks():
    checks = iter([False, True])

    with pytest.raises(OperationCancelled):
        _collect_stream(
            [_text_chunk("first")],
            should_cancel=lambda: next(checks),
        )


def test_cancellable_process_is_killed_without_waiting_for_completion():
    started = time.monotonic()

    with pytest.raises(OperationCancelled):
        run_cancellable_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            should_cancel=lambda: time.monotonic() - started > 0.1,
            timeout=5,
        )

    assert time.monotonic() - started < 2


def test_waiting_for_first_model_chunk_is_aborted_by_cancel():
    class BlockingStream:
        def __init__(self):
            self.closed = threading.Event()

        def __iter__(self):
            return self

        def __next__(self):
            self.closed.wait(5)
            raise RuntimeError("stream closed")

        def close(self):
            self.closed.set()

    stream = BlockingStream()

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: stream)
            )

        def close(self):
            stream.close()

    llm = LLMClient.__new__(LLMClient)
    llm._model = SimpleNamespace(name="fake", api_key="fake", base_url="http://fake")
    llm._client = FakeClient()
    llm._client_lock = threading.Lock()
    started = time.monotonic()

    with pytest.raises(OperationCancelled):
        llm.chat_stream(
            [{"role": "user", "content": "wait"}],
            lambda _text: None,
            should_cancel=lambda: time.monotonic() - started > 0.1,
        )

    assert time.monotonic() - started < 2
