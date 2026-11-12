"""Cache-aware scheduling, independent of Hermes imports and provider credentials."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger("claude_idle_compact")
HUMAN_SOURCES = frozenset({"cli", "tui", "desktop", "telegram", "discord", "slack", "messaging", "local"})
# Compare semantic transcript fields, including tool arguments and signed reasoning.
# Durable row IDs / persistence flags may appear after a SessionDB round trip.
MESSAGE_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning",
                  "reasoning_details", "anthropic_content_blocks", "images")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def transcript_ids(messages: list[dict]) -> tuple[str, ...]:
    return tuple(digest({k: m[k] for k in MESSAGE_FIELDS if k in m}) for m in messages)


def native_claude(*, model: str, provider: str, base_url: str, api_mode: str, platform: str) -> bool:
    url = urlsplit(base_url)
    return (model.startswith("claude-") and provider == "anthropic"
            and api_mode == "anthropic_messages" and platform in HUMAN_SOURCES
            and url.scheme == "https" and url.netloc == "api.anthropic.com"
            and url.path.rstrip("/") in ("", "/v1") and not url.query and not url.fragment)


def one_hour_cache(request: dict) -> bool:
    """Require real 1h markers, refusing mixed/short TTLs and guessed config values."""
    markers = []
    history_markers = []

    def marker(block, *, history=False):
        value = block.get("cache_control") if isinstance(block, dict) else None
        if isinstance(value, dict):
            markers.append(value)
            if history:
                history_markers.append(value)

    marker(request, history=True)  # automatic caching at the end of the conversation
    for block in request.get("system", []) if isinstance(request.get("system"), list) else []:
        marker(block)
    for tool in request.get("tools") or []:
        marker(tool)  # never inspect arbitrary tool schemas or tool arguments
    for message in request.get("messages") or []:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            marker(block, history=True)
    return bool(history_markers) and all(m.get("type") == "ephemeral" and m.get("ttl") == "1h" for m in markers)


def final_answer_matches(capture: Capture, messages: list[dict]) -> bool:
    """Skip host-rewritten answers that the captured provider prefix cannot describe."""
    if not messages or messages[-1].get("role") != "assistant" or messages[-1].get("tool_calls"):
        return False
    text = "\n".join(b.get("text", "") for b in capture.assistant["content"] if b.get("type") == "text").strip()
    final = messages[-1].get("content")
    if isinstance(final, list):
        final = "\n".join(b.get("text", "") for b in final if isinstance(b, dict) and b.get("type") == "text")
    return isinstance(final, str) and bool(text) and text == final.strip()


@dataclass(frozen=True)
class Settings:
    min_prompt_tokens: int = 200_000
    prepare_fraction: float = .9
    expiry_margin_seconds: float = 30
    max_summary_tokens: int = 8_000
    request_timeout_seconds: float = 180
    min_cache_hit_ratio: float = .8

    def __post_init__(self):
        if not 0 < self.prepare_fraction < 1:
            raise ValueError("prepare_fraction must be between 0 and 1")
        if self.min_prompt_tokens < 1 or self.max_summary_tokens < 1024:
            raise ValueError("token limits must be positive; summary limit must be >= 1024")
        if not 0 < self.expiry_margin_seconds < 3600 * (1 - self.prepare_fraction):
            raise ValueError("expiry margin must leave time after the scheduled preparation")
        if not 0 < self.request_timeout_seconds <= 600 or not 0 <= self.min_cache_hit_ratio <= 1:
            raise ValueError("invalid timeout or cache hit ratio")


@dataclass
class Capture:
    request: dict
    assistant: dict
    started_at: float
    route: dict
    tokens: int
    completion_tokens: int
    started_wall_at: float | None = None


@dataclass
class Ready:
    identities: tuple[str, ...]
    text: str
    owner: str
    usage: dict


@dataclass
class State:
    generation: int = 0
    capture: Capture | None = None
    ready: Ready | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    busy: bool = False
    status: str = "waiting_for_turn"


class Controller:
    def __init__(self, *, clock: Callable = time.monotonic, spawn: Callable,
                 wall_clock: Callable = time.time, max_sessions: int = 32):
        self.clock, self.wall_clock, self.spawn = clock, wall_clock, spawn
        self.max_sessions = max_sessions
        self.states: dict[tuple[str, str], State] = {}
        self.lock = threading.RLock()
        self.slot = threading.BoundedSemaphore(1)

    def _state(self, key) -> State:
        if key not in self.states:
            if len(self.states) >= self.max_sessions:
                # Losing a preparation is safe; never retain unlimited conversation snapshots.
                self.close(next(iter(self.states)))
            self.states[key] = State()
        return self.states[key]

    def begin_turn(self, key) -> None:
        with self.lock:
            state = self._state(key)
            state.cancel.set()
            state.generation += 1
            state.busy = True
            state.capture = None
            # A completed candidate can still be applied in this turn's preflight.
            state.status = "ready" if state.ready else "active"

    def begin_request(self, key) -> int:
        with self.lock:
            self.begin_turn(key)
            state = self.states[key]
            state.ready = None  # preflight did not adopt it; this request supersedes it
            state.status = "active"
            return state.generation

    def capture(self, key, generation: int, capture: Capture) -> None:
        with self.lock:
            state = self.states.get(key)
            if state and state.generation == generation:
                state.capture = copy.deepcopy(capture)

    def arm(self, key, *, messages: list[dict], owner: str, settings: Settings,
            context_length: int, instruction: str, credential_fingerprint: str,
            summarize: Callable) -> bool:
        with self.lock:
            state = self.states.get(key)
            if not state:
                return False
            state.busy = False
            capture = state.capture
            if not capture or not one_hour_cache(capture.request):
                state.status = "skipped_no_one_hour_cache"
                return False
            if not final_answer_matches(capture, messages):
                state.status = "skipped_changed_final_answer"
                return False
            if capture.tokens < settings.min_prompt_tokens:
                state.status = "skipped_small_context"
                return False
            # Include the just-generated assistant answer and a summary output reserve.
            thinking_budget = int((capture.request.get("thinking") or {}).get("budget_tokens", 0))
            if thinking_budget > settings.max_summary_tokens - 1024:
                state.status = "skipped_thinking_budget"
                return False
            reserve = settings.max_summary_tokens
            if context_length <= capture.tokens + capture.completion_tokens + reserve + 4096:
                state.status = "skipped_context_headroom"
                return False
            deadline = capture.started_at + 3600 - settings.expiry_margin_seconds
            # Some system monotonic clocks pause during sleep. Wall-clock expiry
            # independently prevents a cold request after waking a laptop.
            started_wall = capture.started_wall_at
            if started_wall is None:
                state.status = "skipped_missing_request_clock"
                return False
            wall_deadline = started_wall + 3600 - settings.expiry_margin_seconds
            if self.clock() >= deadline or self.wall_clock() >= wall_deadline:
                state.status = "skipped_expired"
                return False
            identities = transcript_ids(messages)
            generation = state.generation
            state.cancel.set()
            state.cancel = cancel = threading.Event()
            state.ready = None
            state.status = "scheduled"
            due = capture.started_at + 3600 * settings.prepare_fraction
            wall_due = started_wall + 3600 * settings.prepare_fraction

        def work():
            if cancel.wait(max(0, min(due - self.clock(), wall_due - self.wall_clock()))):
                return
            self.prepare(key, generation=generation, owner=owner, capture=capture,
                         identities=identities, settings=settings, deadline=deadline,
                         wall_deadline=wall_deadline,
                         instruction=instruction, credential_fingerprint=credential_fingerprint,
                         summarize=summarize, cancel=cancel)

        self.spawn(work, name="claude-idle-prepare")
        log.info("Claude idle summary scheduled session=%s delay_seconds=%.0f prompt_tokens=%d",
                 key[1], max(0, due - self.clock()), capture.tokens)
        return True

    def prepare(self, key, *, generation, owner, capture, identities, settings, deadline, wall_deadline,
                instruction, credential_fingerprint, summarize, cancel) -> None:
        def current():
            state = self.states.get(key)
            return (state is not None and state.generation == generation
                    and not state.busy and not cancel.is_set())

        with self.lock:
            if not current() or self.clock() >= deadline or self.wall_clock() >= wall_deadline:
                if current():
                    self.states[key].status = "skipped_expired"
                return
            if not self.slot.acquire(blocking=False):
                self.states[key].status = "skipped_busy_worker"
                return
            self.states[key].status = "preparing"
        try:
            text, usage = summarize(capture=capture, instruction=instruction, settings=settings,
                                    cancel=cancel, deadline=deadline,
                                    wall_deadline=wall_deadline,
                                    credential_fingerprint=credential_fingerprint)
            # An observed miss is never followed by a second, cold summarization attempt.
            total = sum(int(usage.get(k) or 0) for k in ("cache_read_tokens", "cache_write_tokens", "input_tokens"))
            ratio = int(usage.get("cache_read_tokens") or 0) / total if total else 0
            if not text.strip() or ratio < settings.min_cache_hit_ratio:
                raise ValueError("summary empty or observed cache hit ratio too low")
            with self.lock:
                if current():
                    self.states[key].ready = Ready(identities, text, owner, usage)
                    self.states[key].status = "ready"
                    log.info("Claude idle summary ready session=%s cache_hit_ratio=%.3f output_tokens=%s",
                             key[1], ratio, usage.get("output_tokens", 0))
        except Exception as exc:
            with self.lock:
                if current():
                    self.states[key].status = "preparation_failed"
            # Provider exceptions may contain message bodies or credentials: log only type.
            log.warning("Claude idle summary abandoned session=%s error_type=%s", key[1], type(exc).__name__)
        finally:
            self.slot.release()

    def ready_for(self, key, messages, owner, *, take=False) -> Ready | None:
        with self.lock:
            state = self.states.get(key)
            ready = state.ready if state else None
            if not ready or ready.owner != owner:
                return None
            ids = transcript_ids(messages)
            # The only permissible suffix is this return turn's single user message.
            valid = (ids == ready.identities or
                     (len(ids) == len(ready.identities) + 1 and messages[-1].get("role") == "user"
                      and ids[:-1] == ready.identities))
            if not valid:
                state.ready = None
                state.status = "discarded_changed_transcript"
                return None
            if take:
                state.ready = None
                state.status = "adopted"
            return ready

    def close(self, key):
        with self.lock:
            state = self.states.pop(key, None)
            if state:
                state.cancel.set()

    def close_all(self):
        with self.lock:
            for key in list(self.states):
                self.close(key)

    def status(self, key):
        with self.lock:
            state = self.states.get(key)
            return state.status if state else "inactive"
