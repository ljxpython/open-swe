from types import SimpleNamespace
from typing import Any

from agent.utils import langfuse


class _Graph:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    def with_config(self, config: dict[str, Any]) -> "_Graph":
        self.config = config
        return self


def test_with_langfuse_tracing_is_disabled_without_explicit_switch(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPEN_SWE_LANGFUSE_ENABLED", raising=False)
    graph = _Graph()

    assert langfuse.with_langfuse_tracing(graph, {"configurable": {}}, "open-swe-agent") is graph
    assert graph.config is None


def test_with_langfuse_tracing_adds_callback_and_trace_metadata(monkeypatch) -> None:
    monkeypatch.setenv("OPEN_SWE_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    callback = object()
    monkeypatch.setattr(langfuse, "_make_callback", lambda: callback)
    graph = _Graph()

    langfuse.with_langfuse_tracing(
        graph,
        {
            "configurable": {
                "thread_id": "thread-1",
                "github_login": "octocat",
                "source": "dashboard",
            }
        },
        "open-swe-agent",
    )

    assert graph.config == {
        "callbacks": [callback],
        "metadata": {
            "langfuse_trace_name": "open-swe-agent",
            "langfuse_session_id": "thread-1",
            "langfuse_user_id": "octocat",
            "langfuse_tags": ["open-swe-agent", "dashboard"],
        },
    }


def test_redact_removes_tokens_and_sensitive_mapping_values() -> None:
    assert langfuse._redact(
        {"Authorization": "Bearer secret", "nested": "ghp_abcdefghijklmnopqrstuvwxyz"}
    ) == {"Authorization": "[REDACTED]", "nested": "[REDACTED]"}


def test_redact_removes_serialized_authorization_value() -> None:
    assert langfuse._redact('{"Authorization": "Bearer secret"}') == (
        '{"Authorization": "[REDACTED]"}'
    )


def test_mask_otel_spans_replaces_secret_attributes() -> None:
    result = langfuse._mask_otel_spans(
        params=SimpleNamespace(
            spans={
                "span": SimpleNamespace(attributes={"input": '{"token": "secret", "safe": "ok"}'})
            }
        )
    )

    assert result.span_patches["span"].set_attributes == {
        "input": '{"token": "[REDACTED]", "safe": "ok"}'
    }
