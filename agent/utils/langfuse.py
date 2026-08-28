"""Optional Langfuse tracing for server-side LangChain executions."""

import os
import re
from collections.abc import Mapping
from typing import Any

from langgraph.graph.state import RunnableConfig
from langgraph.pregel import Pregel

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\"'\s,}]+"),
    re.compile(
        r'(?i)("(?:authorization|cookie|set-cookie|token|access_token|api_key|x-api-key|secret|client_secret|password)"\s*:\s*")[^"]*'
    ),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9_]+\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]+\b"),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]+\b"),
)
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "api_key",
    "x-api-key",
    "secret",
    "client_secret",
    "password",
}


def _enabled() -> bool:
    return (
        os.getenv("OPEN_SWE_LANGFUSE_ENABLED", "").lower() == "true"
        and bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
        and bool(os.getenv("LANGFUSE_SECRET_KEY"))
    )


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", value)
        return value
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _mask_otel_spans(*, params: Any) -> Any:
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        replacements = {
            key: _redact(value)
            for key, value in span.attributes.items()
            if isinstance(value, str) and _redact(value) != value
        }
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)

    return MaskOtelSpansResult(span_patches=patches) if patches else None


def _make_callback() -> Any:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(mask_otel_spans=_mask_otel_spans)
    return CallbackHandler()


def with_langfuse_tracing(
    graph: Pregel,
    config: RunnableConfig,
    project_name: str,
) -> Pregel:
    """Add a Langfuse callback when tracing is explicitly configured."""
    if not _enabled():
        return graph

    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        configurable = {}

    metadata: dict[str, Any] = {"langfuse_trace_name": project_name}
    thread_id = configurable.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        metadata["langfuse_session_id"] = thread_id
    github_login = configurable.get("github_login")
    if isinstance(github_login, str) and github_login:
        metadata["langfuse_user_id"] = github_login
    source = configurable.get("source")
    metadata["langfuse_tags"] = (
        [project_name, source] if isinstance(source, str) else [project_name]
    )

    return graph.with_config({"callbacks": [_make_callback()], "metadata": metadata})
