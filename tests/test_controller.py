import copy
import unittest
from support import Harness, capture, USAGE
from claude_idle_plugin.controller import Settings, native_claude, one_hour_cache


class SchedulingTests(unittest.TestCase):
    def test_prepare_at_ninety_percent_of_request_start_without_mutating_transcript(self):
        h = Harness()
        before = copy.deepcopy(h.messages)
        self.assertTrue(h.arm())
        self.assertFalse(h.calls)
        self.assertEqual(h.controller.status(h.key), "scheduled")
        h.fire()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0]["deadline"], 3670)
        self.assertEqual(h.controller.status(h.key), "ready")
        self.assertEqual(h.messages, before)

    def test_return_cancels_timer(self):
        h = Harness()
        h.arm()
        h.controller.begin_turn(h.key)
        h.fire()
        self.assertFalse(h.calls)

    def test_new_request_resets_timer_and_stale_request_cannot_publish(self):
        h = Harness()
        h.arm()
        old = h.controller.states[h.key].generation
        h.now = 500
        h.arm(captured=capture(start=500))
        h.controller.capture(h.key, old, capture(start=1))
        h.now = 3740
        for job in h.jobs:
            job()
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0]["capture"].started_at, 500)

    def test_activity_during_summary_discards_result(self):
        h = Harness()
        def during(**kwargs):
            h.controller.begin_turn(h.key)
            return "now stale", USAGE
        h.arm(summary=during)
        h.fire()
        self.assertIsNone(h.controller.ready_for(h.key, h.messages, "owner"))

    def test_ready_survives_return_and_is_consumed_once_with_new_user_message(self):
        h = Harness()
        h.arm()
        h.fire()
        h.controller.begin_turn(h.key)
        returning = h.messages + [{"role": "user", "content": "continue"}]
        self.assertIsNotNone(h.controller.ready_for(h.key, returning, "owner", take=True))
        self.assertIsNone(h.controller.ready_for(h.key, returning, "owner", take=True))

    def test_edits_rewinds_and_appended_assistant_discard_summary(self):
        cases = [lambda m: [{**m[0], "content": "changed"}, m[1]],
                 lambda m: m[:-1], lambda m: m + [{"role": "assistant", "content": "new"}]]
        for change in cases:
            with self.subTest(change=change):
                h = Harness()
                h.arm()
                h.fire()
                self.assertIsNone(h.controller.ready_for(h.key, change(h.messages), "owner"))

    def test_persistence_flags_do_not_invalidate_identical_messages(self):
        h = Harness()
        h.arm()
        h.fire()
        messages = [{**m, "message_uid": "uid", "_db_persisted": True} for m in h.messages]
        self.assertIsNotNone(h.controller.ready_for(h.key, messages, "owner"))

    def test_profiles_and_engine_instances_do_not_share_candidates(self):
        h = Harness()
        h.arm()
        h.fire()
        self.assertIsNone(h.controller.ready_for(("/other/profile", "s1"), h.messages, "owner"))
        self.assertIsNone(h.controller.ready_for(h.key, h.messages, "other-owner"))

    def test_expired_timer_after_sleep_makes_no_request(self):
        h = Harness()
        h.arm()
        h.now = 5000
        h.jobs[0]()
        self.assertFalse(h.calls)
        self.assertEqual(h.controller.status(h.key), "skipped_expired")

    def test_wall_clock_blocks_cold_request_when_monotonic_clock_paused_during_sleep(self):
        h = Harness()
        h.arm()
        h.now = 110  # only ten seconds of uptime elapsed
        h.wall_offset = 7200  # but laptop slept for two hours
        h.jobs[0]()
        self.assertFalse(h.calls)
        self.assertEqual(h.controller.status(h.key), "skipped_expired")

    def test_failure_or_cold_result_has_no_retry_or_ready_candidate(self):
        for summary in (lambda **kw: ("handoff", {"input_tokens": 250_000}),
                        lambda **kw: ("", USAGE)):
            h = Harness()
            h.arm(summary=summary)
            h.fire()
            self.assertEqual(h.controller.status(h.key), "preparation_failed")
            self.assertIsNone(h.controller.ready_for(h.key, h.messages, "owner"))

    def test_small_context_and_five_minute_cache_are_skipped(self):
        for c in (capture(tokens=1000), capture(ttl="5m")):
            h = Harness()
            self.assertFalse(h.arm(captured=c))
            self.assertFalse(h.jobs)

    def test_insufficient_context_headroom_or_output_budget_is_skipped(self):
        h = Harness()
        self.assertFalse(h.arm(captured=capture(tokens=995_000)))
        too_much_thinking = capture()
        too_much_thinking.request["thinking"] = {"type": "enabled", "budget_tokens": 16_000}
        self.assertFalse(h.arm(captured=too_much_thinking))
        self.assertFalse(h.jobs)

    def test_only_one_preparation_runs_per_process(self):
        h = Harness()
        h.arm()
        h.controller.slot.acquire()
        try:
            h.fire()
        finally:
            h.controller.slot.release()
        self.assertFalse(h.calls)
        self.assertEqual(h.controller.status(h.key), "skipped_busy_worker")

    def test_host_rewritten_answer_is_not_summarized_from_a_stale_raw_response(self):
        h = Harness()
        messages = [h.messages[0], {"role": "assistant", "content": "rewritten by host"}]
        self.assertFalse(h.arm(messages=messages))
        self.assertFalse(h.jobs)

    def test_session_close_and_plugin_unload_cancel_pending_work(self):
        for close_all in (True, False):
            h = Harness()
            h.arm()
            h.controller.close_all() if close_all else h.controller.close(h.key)
            h.fire()
            self.assertFalse(h.calls)
            self.assertEqual(h.controller.status(h.key), "inactive")

    def test_state_retention_is_bounded(self):
        h = Harness()
        h.controller.max_sessions = 2
        for i in range(3):
            h.controller.begin_turn(("p", str(i)))
        self.assertEqual(len(h.controller.states), 2)
        self.assertNotIn(("p", "0"), h.controller.states)


class EligibilityTests(unittest.TestCase):
    def test_only_native_interactive_claude(self):
        route = capture().route
        self.assertTrue(native_claude(**route))
        for k, value in (("platform", "cron"), ("platform", "subagent"), ("platform", "oneshot"),
                         ("platform", "api"), ("model", "gpt-test"), ("provider", "openrouter"),
                         ("api_mode", "chat_completions"), ("base_url", "https://example.com"),
                         ("base_url", "https://api.anthropic.com.evil.test"),
                         ("base_url", "http://api.anthropic.com")):
            with self.subTest(k=k, value=value):
                self.assertFalse(native_claude(**{**route, k: value}))

    def test_mixed_and_implicit_five_minute_markers_refused(self):
        req = capture().request
        self.assertTrue(one_hour_cache(req))
        req["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
        self.assertFalse(one_hour_cache(req))
        self.assertFalse(one_hour_cache({"messages": []}))

    def test_caching_only_the_system_prompt_is_not_enough(self):
        req = capture().request
        req["messages"][0]["content"][0].pop("cache_control")
        self.assertFalse(one_hour_cache(req))

    def test_tool_argument_named_cache_control_is_not_a_cache_marker(self):
        req = capture().request
        req["tools"][0]["input_schema"]["properties"] = {"cache_control": {"type": "object"}}
        self.assertTrue(one_hour_cache(req))

    def test_invalid_configuration(self):
        for kw in ({"prepare_fraction": 1}, {"prepare_fraction": 0}, {"expiry_margin_seconds": 400},
                   {"min_prompt_tokens": 0}, {"max_summary_tokens": 500}, {"min_cache_hit_ratio": 2}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Settings(**kw)


if __name__ == "__main__":
    unittest.main()
