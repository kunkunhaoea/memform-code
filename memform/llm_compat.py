"""Provider-specific output budgets at the serialized Chat Completions boundary."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def provider_name(base_url: str | None) -> str:
    host = (urlparse(base_url or "").hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
    }:
        return "aliyun"
    return "openai_compatible"


def output_token_field(base_url: str | None, requested: str = "auto") -> str:
    if requested not in {"auto", "max_tokens", "max_completion_tokens"}:
        raise ValueError("Unknown output token field")
    if requested != "auto":
        return requested
    return "max_tokens" if provider_name(base_url) == "deepseek" else "max_completion_tokens"


def thinking_parameters(base_url: str | None, mode: str = "default") -> dict[str, Any]:
    if mode not in {"default", "on", "off"}:
        raise ValueError("Unknown thinking mode")
    if mode == "default":
        return {}
    provider = provider_name(base_url)
    if provider == "deepseek":
        return {"thinking": {"type": "enabled" if mode == "on" else "disabled"}}
    if provider == "aliyun":
        return {"enable_thinking": mode == "on"}
    raise ValueError("Explicit thinking controls require a supported DeepSeek or Aliyun endpoint")


def make_chat_client(kwargs: dict[str, Any], *, cap: int | None, field: str):
    from langchain_openai import ChatOpenAI

    if cap is None:
        return ChatOpenAI(**kwargs)

    class BudgetedChatOpenAI(ChatOpenAI):
        def _get_request_payload(self, *args, **call_kwargs):
            payload = super()._get_request_payload(*args, **call_kwargs)
            # LangChain renames max_tokens to max_completion_tokens. Override after
            # that conversion, and send exactly one provider-supported limit field.
            payload.pop("max_tokens", None)
            payload.pop("max_completion_tokens", None)
            payload[field] = cap
            return payload

    return BudgetedChatOpenAI(**kwargs)
