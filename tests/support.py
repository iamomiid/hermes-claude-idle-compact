import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "claude_idle_plugin"
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)

from claude_idle_plugin.controller import Capture, Controller, Settings


def capture(start=100, ttl="1h", tokens=250_000):
    return Capture(
        {"model": "claude-test", "system": [{"type": "text", "text": "stable system",
                                            "cache_control": {"type": "ephemeral", "ttl": ttl}}],
         "messages": [{"role": "user", "content": [{"type": "text", "text": "question",
                       "cache_control": {"type": "ephemeral", "ttl": ttl}}]}],
         "tools": [{"name": "read", "description": "stable tool", "input_schema": {"type": "object"}}],
         "thinking": {"type": "adaptive"}, "max_tokens": 16384},
        {"role": "assistant", "content": [{"type": "text", "text": "latest answer"}]}, start,
        {"model": "claude-test", "provider": "anthropic", "base_url": "https://api.anthropic.com",
         "api_mode": "anthropic_messages", "platform": "tui"}, tokens, 500, start)


USAGE = {"cache_read_tokens": 250_000, "cache_write_tokens": 100, "input_tokens": 10, "output_tokens": 1000}


class Harness:
    def __init__(self):
        self.now = 100
        self.wall_offset = 0
        self.jobs = []
        self.controller = Controller(clock=lambda: self.now, wall_clock=lambda: self.now + self.wall_offset,
                                     spawn=lambda target, **kw: self.jobs.append(target))
        self.key = ("/isolated/profile", "s1")
        self.messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "latest answer"}]
        self.calls = []

    def summary(self, **kwargs):
        self.calls.append(kwargs)
        return "A complete handoff", dict(USAGE)

    def arm(self, *, captured=None, settings=None, messages=None, summary=None):
        c = self.controller
        generation = c.begin_request(self.key)
        c.capture(self.key, generation, captured or capture())
        return c.arm(self.key, messages=messages or self.messages, owner="owner", settings=settings or Settings(),
                     context_length=1_000_000, instruction="summarize", credential_fingerprint="fingerprint",
                     summarize=summary or self.summary)

    def fire(self):
        self.now = 3340
        self.jobs.pop(0)()
