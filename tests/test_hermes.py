"""Real Hermes engine / loader / SDK contracts; no network or real credentials."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ROOT, Harness, capture, USAGE
from claude_idle_plugin import hermes_adapter as host
from claude_idle_plugin.controller import Ready, Settings, transcript_ids

try:
    from agent.context_compressor import ContextCompressor
    HERMES = True
except ImportError:
    HERMES = False


@unittest.skipUnless(HERMES, "Run through the Hermes runtime for integration tests")
class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-idle-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = {"compression": {"threshold": .5, "target_ratio": .2, "protect_first_n": 1,
                                        "protect_last_n": 2, "tail_mode": "lean"},
                       "context": {"engine": host.ENGINE_NAME}}
        patcher = patch.object(host, "load_config", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(host, "home_key", return_value=str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, h):
        from claude_idle_plugin.engine import create_engine
        engine = create_engine(h.controller)
        engine.update_model("claude-test", 1_000_000, "https://api.anthropic.com", "fake-key",
                            "anthropic", "anthropic_messages", max_tokens=16384)
        engine.bind_session_state(session_id="s1")
        return engine

    def messages(self):
        return [{"role": "system", "content": "stable system"}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}: " + "old work " * 900}
            for i in range(80)]

    def test_engine_clone_is_independent_and_preserves_host_settings(self):
        h = Harness()
        first = self.engine(h)
        second = first.clone_for_agent()
        second.update_model("claude-other", 200_000, api_mode="anthropic_messages")
        self.assertEqual(first.model, "claude-test")
        self.assertEqual(first.max_tokens, 16384)
        self.assertEqual(first.protect_first_n, 1)
        self.assertNotEqual(first._idle_owner, second._idle_owner)

    def test_turn_completion_arms_only_after_real_request_with_warm_cache(self):
        h = Harness()
        engine = self.engine(h)
        key = (str(self.home), "s1")
        generation = h.controller.begin_request(key)
        captured = capture()
        captured.assistant["content"][0]["text"] = self.messages()[-1]["content"]
        h.controller.capture(key, generation, captured)
        with patch.object(host, "summarize", h.summary):
            engine.on_turn_complete(self.messages(), usage={"prompt_tokens": 250_000}, interrupted=False, failed=False)
        self.assertEqual(h.controller.status(key), "scheduled")
        h.now = 3340
        h.jobs[0]()
        self.assertEqual(h.controller.status(key), "ready")
        self.assertIn("including the last assistant answer", h.calls[0]["instruction"])

    def test_prepared_summary_adopted_by_real_compressor_without_llm_or_transcript_mutation(self):
        h = Harness()
        engine = self.engine(h)
        messages = self.messages()
        key = (str(self.home), "s1")
        state = h.controller._state(key)
        state.ready = Ready(transcript_ids(messages), "## Historical Task Snapshot\nCompleted work.\n\n"
                            "## Active State\nThe project is ready to continue.", engine._idle_owner, USAGE)
        returning = messages + [{"role": "user", "content": "the newest question must survive"}]
        before = copy.deepcopy(returning)
        self.assertTrue(engine.should_compress_preflight(returning))
        # Any auxiliary route used by accident is a test failure, not a real paid call.
        with patch.object(ContextCompressor, "_call_summary_llm", side_effect=AssertionError("unexpected LLM call")):
            result = engine.compress(returning, current_tokens=250_000)
        self.assertEqual(returning, before)
        self.assertLess(len(result), len(returning))
        self.assertTrue(result[0]["content"].startswith("stable system"))
        self.assertTrue(any("newest question" in str(m.get("content")) for m in result))
        self.assertTrue(any("project is ready" in str(m.get("content")) for m in result))
        self.assertEqual(h.controller.status(key), "adopted")
        self.assertEqual(engine.compression_count, 1)

    def test_changed_history_does_not_trigger_idle_compaction(self):
        h = Harness()
        engine = self.engine(h)
        messages = self.messages()
        key = (str(self.home), "s1")
        h.controller._state(key).ready = Ready(transcript_ids(messages), "summary", engine._idle_owner, USAGE)
        messages[2]["content"] = "edited"
        self.assertFalse(engine.should_compress_preflight(messages))

    def test_model_switch_and_reset_discard_preparation(self):
        for reset in (True, False):
            h = Harness()
            engine = self.engine(h)
            key = (str(self.home), "s1")
            h.controller.begin_turn(key)
            if reset:
                engine.on_session_reset()
            else:
                engine.update_model("gpt-test", 200_000, provider="openai", api_mode="chat_completions")
            self.assertEqual(h.controller.status(key), "inactive")

    def test_manual_focused_and_memory_compactions_do_not_substitute_idle_summary(self):
        for kwargs in ({"force": True}, {"focus_topic": "specific task"}, {"memory_context": "new memory"},
                       {"bypass_cooldown": True}):
            with self.subTest(kwargs=kwargs):
                h = Harness()
                engine = self.engine(h)
                messages = self.messages()
                key = (str(self.home), "s1")
                h.controller._state(key).ready = Ready(transcript_ids(messages), "idle summary", engine._idle_owner, USAGE)
                with patch.object(ContextCompressor, "compress", return_value=messages) as compress:
                    self.assertIs(engine.compress(messages, **kwargs), messages)
                compress.assert_called_once()
                self.assertIsNone(engine._idle_summary)
                self.assertEqual(h.controller.status(key), "inactive")

    def test_checkpoint_and_external_memory_provider_disable_preparation(self):
        for key, value in (("compression", {"checkpoint_required": True}), ("memory", {"provider": "external"})):
            with self.subTest(key=key):
                h = Harness()
                engine = self.engine(h)
                session_key = (str(self.home), "s1")
                h.controller.begin_request(session_key)
                original = copy.deepcopy(self.config)
                self.config[key] = {**self.config.get(key, {}), **value}
                engine.on_turn_complete(self.messages(), usage={"prompt_tokens": 250_000})
                self.assertFalse(h.jobs)
                self.assertEqual(h.controller.status(session_key), "inactive")
                self.config.clear()
                self.config.update(original)

    def test_warm_body_preserves_entire_prefix_and_final_answer(self):
        original = capture()
        before = copy.deepcopy(original)
        body = host.build_summary_request(original, "summarize now", Settings())
        self.assertEqual(body["system"], before.request["system"])
        self.assertEqual(body["tools"], before.request["tools"])
        self.assertEqual(body["thinking"], before.request["thinking"])
        self.assertEqual(body["messages"][:-2], before.request["messages"])
        self.assertEqual(body["messages"][-2], before.assistant)
        self.assertEqual(original, before)

    def test_native_response_capture_includes_all_input_buckets_and_sanitizes_output_fields(self):
        response = SimpleNamespace(stop_reason="end_turn", content=[
            {"type": "text", "text": "final", "citations": None, "parsed_output": {"sdk": True}}],
            usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=200, cache_creation_input_tokens=30,
                                  output_tokens=50))
        req = {**capture().request, "extra_headers": {"Authorization": "do-not-retain", "anthropic-beta": "beta"}}
        result = host.capture_response(req, response, started_at=100, route=capture().route)
        self.assertEqual(result.tokens, 240)
        self.assertEqual(result.assistant["content"], [{"type": "text", "text": "final"}])
        self.assertNotIn("Authorization", result.request["extra_headers"])
        response.stop_reason = "max_tokens"
        self.assertIsNone(host.capture_response(req, response, started_at=100, route=capture().route))

    def test_oauth_headers_and_summary_request_use_real_host_sdk_with_mock_http(self):
        import anthropic
        import httpx
        from agent import anthropic_adapter, anthropic_credentials
        real_builder = anthropic_adapter.build_anthropic_client
        real_create = anthropic_adapter.create_anthropic_message
        token = "sk-ant-oat01-fake-test-credential"
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "msg_test", "type": "message", "role": "assistant",
                "model": "claude-test", "content": [{"type": "text", "text": "A complete checkpoint"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 250_000,
                          "cache_creation_input_tokens": 100}})

        def builder(key, base_url, **kwargs):
            original = real_builder(key, base_url, **kwargs)
            client = original.with_options(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            original.close()
            return client

        def create(client, body, **kwargs):
            return real_create(client, body, prefer_stream=False, **kwargs)

        with patch.object(anthropic_credentials, "resolve_anthropic_token", return_value=token), \
             patch.object(anthropic_adapter, "_require_sdk", return_value=anthropic), \
             patch.object(anthropic_adapter, "build_anthropic_client", side_effect=builder), \
             patch.object(anthropic_adapter, "create_anthropic_message", side_effect=create), \
             patch.object(anthropic_adapter, "_get_claude_code_version", return_value="test"):
            text, usage = host.summarize(capture=capture(), instruction="summarize", settings=Settings(),
                cancel=threading.Event(), deadline=time.monotonic() + 60,
                wall_deadline=time.time() + 60,
                credential_fingerprint=host.credential_fingerprint(token))
        self.assertEqual(text, "A complete checkpoint")
        self.assertEqual(usage["cache_read_tokens"], 250_000)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["authorization"], "Bearer " + token)
        self.assertNotIn("x-api-key", requests[0].headers)
        self.assertIn("oauth-2025-04-20", requests[0].headers["anthropic-beta"])
        sent = json.loads(requests[0].content)
        self.assertEqual(sent["system"], capture().request["system"])
        self.assertEqual(sent["messages"][-2]["content"][0]["text"], "latest answer")

    def test_credential_rotation_aborts_before_building_client(self):
        from agent import anthropic_adapter, anthropic_credentials
        with patch.object(anthropic_credentials, "resolve_anthropic_token", return_value="new-key"), \
             patch.object(anthropic_adapter, "build_anthropic_client") as builder, self.assertRaises(RuntimeError):
            host.summarize(capture=capture(), instruction="summary", settings=Settings(), cancel=threading.Event(),
                           deadline=time.monotonic()+60, wall_deadline=time.time()+60,
                           credential_fingerprint=host.credential_fingerprint("old-key"))
        builder.assert_not_called()

    def test_cancellation_during_client_setup_closes_client_without_sending(self):
        from agent import anthropic_adapter, anthropic_credentials
        cancel = threading.Event()
        fake_client = unittest.mock.Mock()
        def builder(*args, **kwargs):
            cancel.set()
            return fake_client
        with patch.object(anthropic_credentials, "resolve_anthropic_token", return_value="fake-key"), \
             patch.object(anthropic_adapter, "build_anthropic_client", side_effect=builder), \
             patch.object(anthropic_adapter, "create_anthropic_message") as create, self.assertRaises(TimeoutError):
            host.summarize(capture=capture(), instruction="summary", settings=Settings(), cancel=cancel,
                           deadline=time.monotonic()+60, wall_deadline=time.time()+60,
                           credential_fingerprint=host.credential_fingerprint("fake-key"))
        create.assert_not_called()
        fake_client.close.assert_called_once()

    def test_capture_failure_does_not_repeat_the_users_request(self):
        import claude_idle_plugin as plugin
        response = object()
        h = Harness()
        with patch.object(plugin, "_controller", h.controller), \
             patch.object(host, "capture_response", side_effect=ValueError("bad response")) as capture_response, \
             patch.object(plugin.log, "warning"):
            next_call = unittest.mock.Mock(return_value=response)
            self.assertIs(plugin.execute(capture().request, next_call, session_id="s1", **capture().route), response)
            next_call.assert_called_once()
            capture_response.assert_called_once()

    def test_unselected_engine_does_not_capture_or_schedule_requests(self):
        import claude_idle_plugin as plugin
        h = Harness()
        self.config["context"]["engine"] = "compressor"
        response = object()
        with patch.object(plugin, "_controller", h.controller), patch.object(host, "capture_response") as capture_response:
            next_call = unittest.mock.Mock(return_value=response)
            self.assertIs(plugin.execute(capture().request, next_call, session_id="s1", **capture().route), response)
            next_call.assert_called_once()
            capture_response.assert_not_called()
        self.assertFalse(h.controller.states)

    def test_real_plugin_manager_discovers_and_unloads_plugin(self):
        from hermes_cli.plugins import PluginManager
        plugins = self.home / "plugins"
        plugins.mkdir()
        (plugins / "claude-idle-compact").symlink_to(ROOT, target_is_directory=True)
        (self.home / "config.yaml").write_text("plugins:\n  enabled: [claude-idle-compact]\ncontext:\n  engine: claude_idle_compact\n")
        manager = PluginManager(scope_key=str(self.home))
        self.addCleanup(manager.unload)
        manager.discover_and_load()
        loaded = manager._plugins.get("claude-idle-compact")
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.error)
        self.assertEqual(manager._context_engine.name, host.ENGINE_NAME)
        manager.unload()
        self.assertIsNone(manager._context_engine)


if __name__ == "__main__":
    unittest.main()
