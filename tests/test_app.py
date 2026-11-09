import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

FAKE_LOCAL_TEXT = "<think>hmm</think>Always split aces and eights."
FAKE_REMOTE_TEXT = "Hit on 16 against a dealer 10."


def metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _load_app(monkeypatch, mode):
    for collector in list(REGISTRY._collector_to_names):
        if any(n.startswith("blackjack_") for n in REGISTRY._collector_to_names[collector]):
            REGISTRY.unregister(collector)
    monkeypatch.setenv("PRODUCT_MODE", mode)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    fake_pipe = MagicMock(
        return_value=[{"generated_text": [{"role": "assistant", "content": FAKE_LOCAL_TEXT}]}]
    )
    fake_pipe.tokenizer.encode.return_value = list(range(12))
    fake_tf = types.ModuleType("transformers")
    fake_tf.pipeline = MagicMock(return_value=fake_pipe)
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    sys.modules.pop("app", None)
    return importlib.import_module("app"), fake_pipe


class _Chunk:
    def __init__(self, text):
        self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=text))]


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def chat_completion(self, *a, **k):
        for word in FAKE_REMOTE_TEXT.split(" "):
            yield _Chunk(word + " ")


def token(value="hf_fake"):
    return types.SimpleNamespace(token=value)


@pytest.fixture
def api_app(monkeypatch):
    app, _ = _load_app(monkeypatch, "api")
    monkeypatch.setattr(app, "InferenceClient", _FakeClient)
    return app


@pytest.fixture
def local_app(monkeypatch):
    app, fake_pipe = _load_app(monkeypatch, "local")
    monkeypatch.setattr(app, "InferenceClient", _FakeClient)
    return app, fake_pipe


def chat(app, msg="16 vs 10?", use_local=False, outage=False, hf_token=None, history=None):
    if hf_token is None:
        hf_token = token()
    return list(app.respond(msg, history or [], "sys", 64, 0.7, 0.9, use_local, outage, hf_token))


def test_remote_success_streams_and_counts(api_app):
    out = chat(api_app)
    assert FAKE_REMOTE_TEXT in out[-1] and "Remote API" in out[-1]
    assert metric("blackjack_requests_total", product="api", backend="remote", status="success") == 1
    assert metric("blackjack_output_tokens_total", product="api", backend="remote") == len(
        FAKE_REMOTE_TEXT.split()
    )
    assert metric("blackjack_request_latency_seconds_count", product="api", backend="remote") == 1
    assert metric("blackjack_time_to_first_token_seconds_count", product="api", backend="remote") == 1
    assert metric("blackjack_requests_in_progress", product="api") == 0


def test_not_logged_in_is_rejected(api_app):
    out = chat(api_app, hf_token=False)
    assert "log in" in out[-1]
    assert metric("blackjack_requests_total", product="api", backend="remote", status="no_token") == 1


def test_mocked_login_uses_server_token(api_app, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_server")
    assert api_app._resolve_token(token(api_app.MOCK_OAUTH_TOKEN)) == "hf_server"
    assert "Remote API" in chat(api_app, hf_token=token(api_app.MOCK_OAUTH_TOKEN))[-1]


def test_outage_fails_over_to_local_container(api_app, monkeypatch):
    calls = []

    class FakeGradioClient:
        def __init__(self, url, **k):
            calls.append(url)

        def predict(self, *args, api_name):
            calls.append(api_name)
            return "Split them."

    monkeypatch.setitem(sys.modules, "gradio_client", types.SimpleNamespace(Client=FakeGradioClient))
    out = chat(api_app, outage=True)
    assert "automatically switched to the local model" in out[-1] and "Split them." in out[-1]
    assert calls == [api_app.LOCAL_PRODUCT_URL, "/local_generate"]
    assert metric("blackjack_failovers_total", product="api") == 1
    assert metric("blackjack_errors_total", product="api", backend="remote", error_type="RuntimeError") == 1
    assert metric("blackjack_requests_total", product="api", backend="local", status="success") == 1
    assert metric("blackjack_requests_in_progress", product="api") == 0


def test_history_with_content_parts_is_flattened(api_app):
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "hello", "metadata": {}},
    ]
    assert api_app.build_messages("next", history, "sys") == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]
    chat(api_app, history=history)
    assert metric("blackjack_conversation_turns_sum", product="api") == 2


def test_local_model_strips_thinking_and_counts_tokens(local_app):
    app, fake_pipe = local_app
    out = chat(app, use_local=True, hf_token=False)
    assert "Always split aces and eights." in out[-1] and "<think>" not in out[-1]
    fake_pipe.assert_called_once()
    assert metric("blackjack_requests_total", product="local", backend="local", status="success") == 1
    assert metric("blackjack_output_tokens_total", product="local", backend="local") == 12
    assert metric("blackjack_model_load_seconds", product="local") >= 0


def test_local_failure_is_counted_and_raised(local_app):
    app, fake_pipe = local_app
    fake_pipe.side_effect = RuntimeError("out of memory")
    with pytest.raises(RuntimeError):
        chat(app, use_local=True)
    assert metric("blackjack_errors_total", product="local", backend="local", error_type="RuntimeError") == 1
    assert metric("blackjack_requests_in_progress", product="local") == 0


@pytest.mark.parametrize("mode", ["api", "local"])
def test_ui_builds_with_cs1_controls(monkeypatch, mode):
    app, _ = _load_app(monkeypatch, mode)
    monkeypatch.setattr("gradio.oauth._get_mocked_oauth_info", lambda: {})
    demo = app.build_demo()
    labels = {getattr(b, "label", None) for b in demo.blocks.values()}
    assert {"Use Local Model", "Simulate Remote Outage (Demo Failover)", "Max new tokens"} <= labels
    local_box = next(b for b in demo.blocks.values() if getattr(b, "label", None) == "Use Local Model")
    assert local_box.value == (mode == "local")


def test_mocked_login_never_puts_real_token_in_cookie(api_app, monkeypatch):
    monkeypatch.setattr(
        api_app, "_real_mocked_oauth_info", lambda: {"access_token": "hf_real", "userinfo": {}}
    )
    assert api_app._mocked_oauth_info_without_token()["access_token"] == api_app.MOCK_OAUTH_TOKEN
