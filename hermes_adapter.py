"""The small, explicitly tested boundary to Hermes' native Claude implementation.

No token files, environment credentials, HTTP authentication headers, or custom
provider clients live in this plugin. Hermes resolves and constructs the client.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import logging
import time
from dataclasses import fields

from .controller import Capture, Settings

PLUGIN_ID = "claude-idle-compact"
ENGINE_NAME = "claude_idle_compact"
log = logging.getLogger("claude_idle_compact")


def home_key() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home().resolve())


def settings_from(config: dict) -> Settings:
    raw = (((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}).get("settings") or {}
    known = {f.name for f in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError("Unknown claude-idle-compact settings: " + ", ".join(sorted(unknown)))
    return Settings(**raw)


def load_config() -> dict:
    from hermes_cli.config import load_config_readonly
    return load_config_readonly() or {}


def spawn(target, *, name):
    from agent.memory_provider import spawn_context_thread
    thread = spawn_context_thread(target, name=name, daemon=True)
    thread.start()
    return thread


def check_compatibility():
    from agent.context_compressor import ContextCompressor
    from agent.context_engine import ContextEngine
    from agent.anthropic_adapter import build_anthropic_client, create_anthropic_message
    from agent.anthropic_credentials import resolve_anthropic_token
    from agent.anthropic_message_convert import _sanitize_replay_block
    # Protected compressor seams are isolated here and covered against the real host.
    for name, parameters in {
        "_call_summary_llm": ("prompt", "prompt_started_at"),
        "_build_summary_prompt": ("content_to_summarize", "summary_budget", "focus_topic", "memory_context", "has_user_turn"),
        "_on_summary_failure": ("e", "turns_to_summarize", "focus_topic", "memory_context"),
    }.items():
        actual = inspect.signature(getattr(ContextCompressor, name)).parameters
        if not all(p in actual for p in parameters):
            raise RuntimeError(f"Incompatible Hermes compressor: {name}")
    if not callable(getattr(ContextEngine, "on_turn_complete", None)):
        raise RuntimeError("Hermes lacks context-engine turn completion")
    return ContextCompressor


def credential_fingerprint(api_key) -> str:
    # A refreshed/different credential is deliberately a skipped attempt, never an
    # opportunity to accidentally spend from another account in the profile's pool.
    return hashlib.sha256(api_key.encode()).hexdigest() if isinstance(api_key, str) and api_key else ""


def capture_response(request: dict, response, *, started_at: float, route: dict,
                     started_wall_at: float | None = None) -> Capture | None:
    from agent.anthropic_message_convert import _sanitize_replay_block
    if getattr(response, "stop_reason", None) != "end_turn":
        return None  # no tool-loop, refused, interrupted, or truncated answer
    blocks = []
    for block in response.content:
        raw = block if isinstance(block, dict) else block.model_dump(mode="json", exclude_none=True)
        clean = _sanitize_replay_block(raw)
        if clean:
            blocks.append(clean)
    if not blocks or any(b.get("type") == "tool_use" for b in blocks):
        return None
    usage = response.usage
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    fresh = int(getattr(usage, "input_tokens", 0) or 0)
    captured = copy.deepcopy(request)
    # SDK control fields aren't part of the request prefix. Don't keep custom
    # authentication headers that another extension may have injected.
    for key in ("timeout", "stream", "stream_options", "extra_query"):
        captured.pop(key, None)
    if "extra_headers" in captured:
        safe = {"anthropic-beta", "anthropic-version", "x-app", "user-agent"}
        captured["extra_headers"] = {k: v for k, v in captured["extra_headers"].items() if k.lower() in safe}
    return Capture(captured, {"role": "assistant", "content": blocks}, started_at,
                   dict(route), read + written + fresh, int(getattr(usage, "output_tokens", 0) or 0), started_wall_at)


def build_summary_request(capture: Capture, instruction: str, settings: Settings) -> dict:
    body = copy.deepcopy(capture.request)
    # Past system/tools/messages, thinking settings and cache markers stay intact.
    # The final assistant answer is crucial: the last request alone omits it.
    body["messages"] += [copy.deepcopy(capture.assistant), {"role": "user", "content": instruction}]
    thinking_budget = (body.get("thinking") or {}).get("budget_tokens", 0)
    if int(thinking_budget) > settings.max_summary_tokens - 1024:
        raise ValueError("Captured thinking budget leaves insufficient summary output")
    body["max_tokens"] = settings.max_summary_tokens
    body.pop("stream", None)
    return body


def summarize(*, capture, instruction, settings, cancel, deadline, wall_deadline, credential_fingerprint):
    from agent.anthropic_adapter import build_anthropic_client, create_anthropic_message
    from agent.anthropic_credentials import resolve_anthropic_token

    token = resolve_anthropic_token(model=capture.route["model"])
    if not token or not credential_fingerprint:
        raise RuntimeError("No matching host credential")
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    if fingerprint != credential_fingerprint:
        raise RuntimeError("Host credential changed; skip this idle window")
    body = build_summary_request(capture, instruction, settings)
    started = time.monotonic()
    if cancel.is_set() or started >= deadline or time.time() >= wall_deadline:
        raise TimeoutError("Preparation no longer eligible")

    def check_cancel(_event):
        if cancel.is_set() or time.monotonic() - started >= settings.request_timeout_seconds:
            # Hermes' stream helper propagates TimeoutError and closes the stream.
            raise TimeoutError("Idle preparation cancelled")

    client = build_anthropic_client(token, capture.route["base_url"], timeout=settings.request_timeout_seconds)
    try:
        # No provider retries: a retry could turn a warm request into a cold reread.
        client.max_retries = 0
        if cancel.is_set() or time.monotonic() >= deadline or time.time() >= wall_deadline:
            raise TimeoutError("Cache expiry too close")
        response = create_anthropic_message(client, body, on_stream_event=check_cancel)
        usage = response.usage
        counts = {
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        # Main-loop usage ledgers don't see this background call. Keep every
        # completed call's token buckets visible, including misses/rejected output.
        log.info("Claude idle summary usage session=%s model=%s cache_read_tokens=%d cache_write_tokens=%d input_tokens=%d output_tokens=%d",
                 capture.route.get("session_id", "-"), capture.route["model"], counts["cache_read_tokens"], counts["cache_write_tokens"],
                 counts["input_tokens"], counts["output_tokens"])
        if cancel.is_set() or response.stop_reason != "end_turn":
            raise RuntimeError("Incomplete summary")
        if any(b.type == "tool_use" for b in response.content):
            raise RuntimeError("Summary attempted tool use")
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        return text, counts
    finally:
        client.close()


def compressor_options(config: dict) -> dict:
    compression = config.get("compression") or {}
    mapping = {
        "threshold": "threshold_percent", "target_ratio": "summary_target_ratio",
        "protect_first_n": "protect_first_n", "protect_last_n": "protect_last_n",
        "tail_mode": "tail_mode", "min_tail_user_messages": "min_tail_user_messages",
        "abort_on_summary_failure": "abort_on_summary_failure", "model_thresholds": "model_thresholds",
        "threshold_tokens": "threshold_tokens_cap", "proactive_prune_tokens": "proactive_prune_tokens",
        "proactive_prune_min_result_chars": "proactive_prune_min_result_chars",
        "proactive_prune_min_reclaim_tokens": "proactive_prune_min_reclaim_tokens",
    }
    options = {dest: compression[src] for src, dest in mapping.items() if src in compression}
    return {"model": "", "quiet_mode": True, "custom_providers": config.get("custom_providers"), **options}
