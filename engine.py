"""Hermes keeps ownership of foreground compaction and transcript persistence."""
from __future__ import annotations

import logging
import uuid

from . import hermes_adapter as host

log = logging.getLogger("claude_idle_compact")


def create_engine(controller):
    Base = host.check_compatibility()

    class ClaudeIdleEngine(Base):
        @property
        def name(self):
            return host.ENGINE_NAME

        def __init__(self):
            config = host.load_config()
            super().__init__(**host.compressor_options(config))
            self._idle_owner = uuid.uuid4().hex
            self._idle_key = None
            self._idle_summary = None

        def clone_for_agent(self):
            # A fresh compressor for every agent, no copied locks or session state.
            return ClaudeIdleEngine()

        def bind_session_state(self, session_db=None, session_id=""):
            key = (host.home_key(), session_id)
            if self._idle_key and self._idle_key != key:
                controller.close(self._idle_key)
            self._idle_key = key
            super().bind_session_state(session_db=session_db, session_id=session_id)

        def on_session_start(self, session_id, **kwargs):
            key = (host.home_key(), session_id)
            if self._idle_key and self._idle_key != key:
                controller.close(self._idle_key)
            self._idle_key = key
            super().on_session_start(session_id, **kwargs)

        def on_session_end(self, session_id, messages):
            if self._idle_key:
                controller.close(self._idle_key)
            super().on_session_end(session_id, messages)

        def on_session_reset(self):
            if self._idle_key:
                controller.close(self._idle_key)
            self._idle_summary = None
            super().on_session_reset()

        def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode="", max_tokens=None):
            old = (self.model, self.base_url, self.provider, self.api_mode)
            if self._idle_key and old != (model, base_url, provider, api_mode):
                controller.close(self._idle_key)
            super().update_model(model, context_length, base_url, api_key, provider, api_mode, max_tokens=max_tokens)

        def on_turn_complete(self, messages, usage=None, **kwargs):
            super().on_turn_complete(messages, usage, **kwargs)
            key = self._idle_key
            if not key:
                return
            if kwargs.get("interrupted") or kwargs.get("failed") or not usage:
                controller.close(key)
                return
            config = host.load_config()
            # Respect the host's explicit compression and memory-checkpoint gates.
            compression = config.get("compression") or {}
            if (compression.get("enabled", True) is False
                    or compression.get("checkpoint_required", False)
                    or compression.get("require_memory_checkpoint", False)):
                controller.close(key)
                return
            if (config.get("memory") or {}).get("provider") not in (None, "", "default", "builtin", "built-in", "none"):
                controller.close(key)  # provider-supplied handoff context must be collected at compaction time
                return
            try:
                if self.get_active_compression_failure_cooldown() or not self.has_content_to_compress(messages):
                    controller.close(key)
                    return
                if len(messages) <= self.protect_first_n + self.protect_last_n + 1:
                    controller.close(key)  # host's preflight maintenance gate must be reachable
                    return
                settings = host.settings_from(config)
                # Use the host's summary structure without serializing history into a
                # second prompt. The original history is already in the warm request.
                instruction = (
                    "Host context-maintenance request: stop work and write only a checkpoint summary. "
                    "Do not execute tools or answer previous requests. The preceding conversation, "
                    "including the last assistant answer, is the source material.\n\n"
                    + self._build_summary_prompt(
                        "[Source material is the conversation above; do not repeat it verbatim.]",
                        settings.max_summary_tokens, None, "", True)
                )
                controller.arm(key, messages=messages, owner=self._idle_owner, settings=settings,
                               context_length=self.context_length, instruction=instruction,
                               credential_fingerprint=host.credential_fingerprint(self.api_key),
                               summarize=host.summarize)
            except Exception as exc:
                controller.close(key)
                log.warning("Claude idle scheduling skipped error_type=%s", type(exc).__name__)

        def should_compress_preflight(self, messages):
            if self._idle_key and controller.ready_for(self._idle_key, messages, self._idle_owner):
                return True
            return super().should_compress_preflight(messages)

        def compress(self, messages, current_tokens=None, focus_topic=None, force=False,
                     memory_context="", bypass_cooldown=False):
            ready = None
            # Manual/focused/recovery compaction and memory-provider context retain
            # their normal fresh-summary behavior. Never substitute an idle summary.
            fresh = force or focus_topic or bypass_cooldown or memory_context.strip()
            if not fresh and self._idle_key:
                ready = controller.ready_for(self._idle_key, messages, self._idle_owner, take=True)
            if fresh and self._idle_key:
                controller.close(self._idle_key)
            self._idle_summary = ready.text if ready else None
            try:
                return super().compress(
                    messages, current_tokens=current_tokens, focus_topic=focus_topic,
                    # Already paid for the summary; bypass the stock feasibility
                    # heuristic while retaining its window, assembly, and commit gates.
                    force=bool(force or ready), memory_context=memory_context,
                    bypass_cooldown=bypass_cooldown,
                )
            finally:
                self._idle_summary = None

        def _call_summary_llm(self, prompt, prompt_started_at):
            if self._idle_summary is not None:
                return self._idle_summary
            return super()._call_summary_llm(prompt, prompt_started_at)

        def _on_summary_failure(self, error, *args, **kwargs):
            if self._idle_summary is not None:
                # An invalid prepared summary must never become a degraded handoff
                # or trigger a hidden paid retry in this adoption attempt.
                raise RuntimeError("Prepared Claude summary could not be adopted") from error
            return super()._on_summary_failure(error, *args, **kwargs)

        def get_status(self):
            return {**super().get_status(), "claude_idle_compact": controller.status(self._idle_key)}

    return ClaudeIdleEngine()
