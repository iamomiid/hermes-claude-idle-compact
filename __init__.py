"""Standalone Hermes plugin for native Claude sessions only."""
from __future__ import annotations

import logging
import time

from . import hermes_adapter as host
from .controller import Controller, HUMAN_SOURCES, native_claude, one_hour_cache

log = logging.getLogger("claude_idle_compact")
_controller = Controller(spawn=host.spawn)


def enabled_for_profile():
    return (host.load_config().get("context") or {}).get("engine") == host.ENGINE_NAME


def before_turn(session_id="", **kwargs):
    if session_id and enabled_for_profile() and str(kwargs.get("model") or "").startswith("claude-") and kwargs.get("platform") in HUMAN_SOURCES:
        _controller.begin_turn((host.home_key(), session_id))


def execute(request, next_call, session_id="", **context):
    key = (host.home_key(), session_id)
    allowed = session_id and enabled_for_profile() and native_claude(**{
        k: str(context.get(k) or "") for k in ("model", "provider", "base_url", "api_mode", "platform")
    })
    if not allowed:
        _controller.close(key)
        return next_call(request)
    generation = _controller.begin_request(key)
    started_at = time.monotonic()  # TTL is measured from request start, not response completion
    started_wall_at = time.time()
    response = next_call(request)  # host owns all live-turn execution
    try:
        capture = host.capture_response(request, response, started_at=started_at, started_wall_at=started_wall_at,
            route={"session_id": session_id, **{
                k: context[k] for k in ("model", "provider", "base_url", "api_mode", "platform")
            }}) if one_hour_cache(request) else None
        if capture:
            _controller.capture(key, generation, capture)
    except Exception as exc:
        # Capture failures never repeat or fail the user's provider request.
        log.warning("Claude idle capture skipped error_type=%s", type(exc).__name__)
    return response


def session_end(session_id="", **kwargs):
    if session_id:
        _controller.close((host.home_key(), session_id))


def register(ctx):
    from .engine import create_engine
    engine = create_engine(_controller)
    if ctx.register_context_engine(engine) is None:
        log.warning("Claude idle context-engine registration returned no handle; check for an engine conflict")
    ctx.register_middleware("llm_execution", execute)
    ctx.register_hook("pre_llm_call", before_turn)
    ctx.register_hook("on_session_end", session_end)
    ctx.on_unload(_controller.close_all)


def unregister(ctx):
    _controller.close_all()
