"""Verify serialized limits offline, or make exactly ONE small live limit probe.

Offline is the default and uses httpx.MockTransport (no real network or key).
Live mode selects a platform, uses a temporary cache and disables retries.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from getpass import getpass
from pathlib import Path

from memform.common import load_env
from memform.eval.core import CachedChatModel, DiskCache
from memform.llm_compat import output_token_field, provider_name


def choose_provider() -> str:
    print("请选择接口：")
    print("  1. DeepSeek 官方（deepseek-flash）")
    print("  2. 阿里云 DeepSeek（deepseek-v4.1-flash-1017）")
    while True:
        value = input("请输入 1 或 2：").strip()
        if value in {"1", "2"}:
            return "deepseek" if value == "1" else "aliyun"
        print("选择无效，请输入 1 或 2。")


def resolve_target(selected: str, base_url: str | None, model: str | None, environ: dict) -> tuple[str, str]:
    """Bind platform defaults together; never inherit the other platform's model."""
    selected = {"1": "deepseek", "2": "aliyun"}.get(selected, selected)
    active_base = environ.get("OPENAI_BASE_URL") or ""
    if selected == "deepseek":
        base_url = base_url or "https://api.deepseek.com"
        model = model or "deepseek-flash"
    elif selected == "aliyun":
        base_url = (base_url or environ.get("ALIYUN_BASE_URL")
                    or (active_base if provider_name(active_base) == "aliyun" else ""))
        model = model or "deepseek-v4.1-flash-1017"
    elif selected == "env":
        base_url = base_url or active_base
        model = model or environ.get("MEMFORM_MODEL")
    else:
        raise ValueError("Unknown provider selection")
    if not base_url or not model:
        raise ValueError("缺少接口地址或模型。阿里云地址可设置 ALIYUN_BASE_URL 或传 --base-url。")
    if selected != "env" and provider_name(base_url) != selected:
        raise ValueError("所选平台与 --base-url 不匹配；尚未发送请求。")
    return base_url, model


def configured_key(provider: str, base_url: str, environ: dict) -> str | None:
    """Only reuse the generic key when its configured endpoint matches the target."""
    specific = {"deepseek": "DEEPSEEK_API_KEY", "aliyun": "ALIYUN_API_KEY"}.get(provider)
    if specific and environ.get(specific):
        return environ[specific]
    if (environ.get("OPENAI_BASE_URL") or "").rstrip("/") == base_url.rstrip("/"):
        return environ.get("OPENAI_API_KEY")
    return None


def offline_probe(base_url: str, model_name: str, cap: int = 64, thinking_mode: str = "default") -> dict:
    import httpx
    captured = []
    def handle(request):
        body = json.loads(request.content)
        captured.append({k: body[k] for k in ("model", "max_tokens", "max_completion_tokens", "thinking", "enable_thinking") if k in body})
        return httpx.Response(200, json={
            "id": "offline-probe", "object": "chat.completion", "created": 0, "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                      "completion_tokens_details": {"reasoning_tokens": 0 if thinking_mode == "off" else 1}},
        })
    with tempfile.TemporaryDirectory() as temp:
        with contextlib.redirect_stdout(io.StringIO()):
            model = CachedChatModel("probe", model_name, base_url, "offline-placeholder",
                                    DiskCache(Path(temp) / "cache.jsonl"), timeout=None,
                                    max_retries=0, max_output_tokens=cap, thinking_mode=thinking_mode)
            # Use the real LangChain + OpenAI serialization, replacing only transport.
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                from openai import OpenAI
                sdk = OpenAI(api_key="offline-placeholder", base_url=base_url, http_client=client)
                model.client.client = sdk.chat.completions
                model.invoke("Offline transport test.", "No real request.")
        field = output_token_field(base_url)
        assert len(captured) == 1
        assert captured[0].get(field) == cap
        assert len(set(captured[0]) & {"max_tokens", "max_completion_tokens"}) == 1
        return {"mode": "offline", "provider": provider_name(base_url), "wire_parameters": captured[0],
                "actual_api_calls": 0, "status": "wire_verified_server_not_tested",
                "response_metadata": model.last_call_metadata}


def main():
    import os
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--provider", choices=["1", "2", "deepseek", "aliyun", "env"],
                        help="1=DeepSeek, 2=Aliyun; interactive live mode asks when omitted")
    parser.add_argument("--base-url")
    parser.add_argument("--model", help="Explicit model override; otherwise use the selected platform's API ID")
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--output-token-field", choices=["auto", "max_tokens", "max_completion_tokens"], default="auto")
    parser.add_argument("--thinking", choices=["default", "on", "off"], default="off")
    parser.add_argument("--ask-key", action="store_true", help="Read a key privately instead of the current .env key")
    args = parser.parse_args()
    if args.max_output_tokens < 1:
        parser.error("Token cap must be positive")
    if args.mode == "offline":
        for base, name in [("https://api.deepseek.com", "deepseek-flash"),
                           ("https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "deepseek-v4.1-flash-1017")]:
            print(json.dumps(offline_probe(base, name, args.max_output_tokens, args.thinking), ensure_ascii=False))
        return
    interactive_choice = False
    selected = args.provider
    if selected is None:
        if args.base_url:
            detected = provider_name(args.base_url)
            selected = detected if detected in {"deepseek", "aliyun"} else "env"
        elif sys.stdin.isatty():
            try:
                selected = choose_provider()
                interactive_choice = True
            except EOFError:
                parser.error("未完成平台选择，未发送请求。")
        else:
            parser.error("非交互运行请指定 --provider 1/2/env 或 --base-url。")
    if selected in {"2", "aliyun"} and not args.base_url and not os.environ.get("ALIYUN_BASE_URL") and provider_name(os.environ.get("OPENAI_BASE_URL")) != "aliyun":
        if sys.stdin.isatty():
            args.base_url = input("请输入阿里云工作空间的 base_url：").strip()
    try:
        args.base_url, args.model = resolve_target(selected, args.base_url, args.model, os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    provider = provider_name(args.base_url)
    if args.thinking != "default" and provider == "openai_compatible":
        parser.error("Explicit thinking controls are supported here only for DeepSeek and Aliyun")
    print(f"已选择：{provider}；地址：{args.base_url}；模型：{args.model}；思考模式：{args.thinking}")
    key = None if args.ask_key or interactive_choice else configured_key(provider, args.base_url, os.environ)
    if key is None and (args.ask_key or interactive_choice or sys.stdin.isatty()):
        key = getpass("请输入该平台的 API Key（隐藏输入）：").strip()
    if not key:
        parser.error("No API key; set local .env or use --ask-key")
    captured = []
    def capture(request):
        body = json.loads(request.content)
        captured.append({k: body[k] for k in ("model", "max_tokens", "max_completion_tokens", "thinking", "enable_thinking") if k in body})
    with tempfile.TemporaryDirectory() as temp:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                model = CachedChatModel("probe", args.model, args.base_url, key,
                                        DiskCache(Path(temp) / "cache.jsonl"), timeout=120,
                                        max_retries=0, temperature=None,
                                        max_output_tokens=args.max_output_tokens,
                                        output_token_field=args.output_token_field,
                                        thinking_mode=args.thinking)
                model._http_client.event_hooks["request"].append(capture)
                model.invoke("Follow the user's instruction.",
                             "Print the integers from 1 to 100 in order, separated by commas. No explanation or summary.")
            usage, meta = model.last_call_usage, model.last_call_metadata
            tolerance = 10 if provider == "aliyun" else 0
            within = usage is not None and usage["output_tokens"] <= args.max_output_tokens + tolerance
            binding = meta.get("finish_reason") == "length"
            status = "verified_binding_limit" if within and binding else "inconclusive_limit_not_reached" if within else "failed_or_missing_usage"
            if meta.get("thinking_mode_violation"):
                status = "failed_thinking_not_disabled"
            print(json.dumps({"mode": "live", "provider": provider, "wire_parameters": captured,
                              "thinking_mode": args.thinking,
                              "thinking_status": ("disabled_confirmed" if args.thinking == "off" and meta.get("reasoning_tokens") == 0
                                                  else "not_reported" if meta.get("reasoning_tokens") is None else "see_response_metadata"),
                              "actual_api_calls": len(captured), "configured_cap": args.max_output_tokens,
                              "documented_tolerance": tolerance, "usage": usage,
                              "response_metadata": meta, "status": status}, ensure_ascii=False, indent=2))
            if status != "verified_binding_limit":
                raise SystemExit(2)
        except Exception as exc:
            print(json.dumps({"mode": "live", "status": "request_failed", "error_type": type(exc).__name__,
                              "provider": provider, "model": args.model,
                              "http_status": getattr(exc, "status_code", None), "actual_api_calls": len(captured),
                              "wire_parameters": captured}, ensure_ascii=False, indent=2))
            raise SystemExit(1)


if __name__ == "__main__":
    main()
