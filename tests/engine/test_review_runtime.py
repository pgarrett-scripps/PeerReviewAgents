"""Deterministic checks for bounded review model execution."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peerreviewagents.engine import model_runtime as runtime


class ReviewRuntime(unittest.TestCase):
    def test_shared_source_images_accepts_ten_and_never_truncates(self):
        from peerreviewagents.engine.reviewer import shared_source_images
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for index in range(11):
                path = root / f'{index}.png'
                path.write_bytes(str(index).encode())
                images.append(path)
            self.assertEqual(shared_source_images(images[:10]),
                             [path.resolve() for path in images[:10]])
            with self.assertRaisesRegex(ValueError, 'at most 10'):
                shared_source_images(images)

    def test_explicit_model_selection_has_no_automatic_fallback(self):
        from peerreviewagents.engine.reviewer import review
        for model in ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-astra', 'gpt-5.6-luna', 'gpt-5.5'):
            runtime.validate_protocol({**self.protocol, 'model': model})
        with self.assertRaises(runtime.StudyBlocked):
            runtime.validate_protocol({**self.protocol, 'model': 'unknown-model'})
        with self.assertRaisesRegex(ValueError, 'explicitly'):
            review(None, None, None, None, model='unknown-model')
        with self.assertRaisesRegex(ValueError, 'explicitly'):
            review(None, None, None, None, model='claude-fable-5-1')

    def setUp(self):
        self.protocol = {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "held_out_runs_authorized": False,
            "max_calls_per_condition": 2,
            "observed_token_stop": 1000,
            "call_timeout_seconds": 30,
        }
        self.schema = {"type": "object"}

    def successful_run(self, value=None, input_tokens=7, output_tokens=3):
        value = value or {"answer": "ok"}

        def run(command, **kwargs):
            answer = Path(command[command.index("--output-last-message") + 1])
            answer.write_text(json.dumps(value))
            event = {"type": "turn.completed", "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }}
            kwargs["stdout"].write(json.dumps(event) + "\n")
            return SimpleNamespace(returncode=0)

        return run

    def test_success_is_cached_and_accounted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            fake = self.successful_run()
            with patch.object(runtime.subprocess, "run", side_effect=fake) as invoke:
                self.assertEqual(runtime.model_call(self.protocol, out, "prompt", self.schema), {"answer": "ok"})
                self.assertEqual(runtime.model_call(self.protocol, out, "prompt", self.schema), {"answer": "ok"})
            self.assertEqual(invoke.call_count, 1)
            rows = runtime.jsonl(out / "usage.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["observed_tokens"], 10)
            self.assertFalse(rows[0]["usage_unknown"])

    def test_completed_turn_accepts_earlier_reconnect_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def run(command, **kwargs):
                answer = Path(command[command.index("--output-last-message") + 1])
                answer.write_text('{"answer": "recovered transport"}')
                events = [
                    {"type": "thread.started"},
                    {"type": "turn.started"},
                    {"type": "error", "message": "Reconnecting... 4/5"},
                    {"type": "error", "message": "Reconnecting... 5/5"},
                    {"type": "item.started", "item": {"type": "mcp_tool_call", "status": "in_progress"}},
                    {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed"}},
                    {"type": "item.completed", "item": {"type": "agent_message"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 5}},
                ]
                kwargs["stdout"].write("\n".join(json.dumps(event) for event in events) + "\n")
                return SimpleNamespace(returncode=0)

            with patch.object(runtime.subprocess, "run", side_effect=run):
                value = runtime.model_call(self.protocol, out, "prompt", self.schema)
            self.assertEqual(value, {"answer": "recovered transport"})
            receipt = runtime.jsonl(out / "usage.jsonl")[0]
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["observed_tokens"], 17)

    def test_error_after_completed_turn_remains_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def run(command, **kwargs):
                answer = Path(command[command.index("--output-last-message") + 1])
                answer.write_text('{"answer": "must not be accepted"}')
                events = [
                    {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}},
                    {"type": "error", "message": "terminal transport failure"},
                ]
                kwargs["stdout"].write("\n".join(json.dumps(event) for event in events) + "\n")
                return SimpleNamespace(returncode=0)

            with patch.object(runtime.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, "Codex failed"):
                    runtime.model_call(self.protocol, out, "prompt", self.schema)

    def test_turn_failed_before_completed_turn_remains_terminal(self):
        events = [
            {"type": "turn.failed", "error": {"message": "generation failed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}},
        ]
        self.assertEqual(runtime.terminal_codex_failures(events), [events[0]])

    def test_non_reconnect_error_before_completed_turn_remains_terminal(self):
        events = [
            {"type": "error", "message": "authentication failed"},
            {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}},
        ]
        self.assertEqual(runtime.terminal_codex_failures(events), [events[0]])

    def test_malformed_answer_after_completed_turn_remains_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def run(command, **kwargs):
                answer = Path(command[command.index("--output-last-message") + 1])
                answer.write_text("not json")
                event = {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}}
                kwargs["stdout"].write(json.dumps(event) + "\n")
                return SimpleNamespace(returncode=0)

            with patch.object(runtime.subprocess, "run", side_effect=run):
                with self.assertRaises(json.JSONDecodeError):
                    runtime.model_call(self.protocol, out, "prompt", self.schema)
            receipt = runtime.jsonl(out / "usage.jsonl")[0]
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["observed_tokens"], 10)

    def test_recovery_only_cannot_launch_or_change_a_request(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(runtime.subprocess, 'run', side_effect=self.successful_run()) as invoke:
                with self.assertRaisesRegex(runtime.StudyBlocked, 'Recovery never launches'):
                    runtime.model_call(self.protocol, out, 'prompt', self.schema, recover_only=True)
                self.assertEqual(invoke.call_count, 0)
                runtime.model_call(self.protocol, out, 'prompt', self.schema)
                self.assertEqual(runtime.model_call(self.protocol, out, 'prompt', self.schema, recover_only=True), {'answer':'ok'})
                with self.assertRaisesRegex(runtime.StudyBlocked, 'Recovery never launches'):
                    runtime.model_call(self.protocol, out, 'changed prompt', self.schema, recover_only=True)
                self.assertEqual(invoke.call_count, 1)

    def test_failed_call_with_unknown_usage_blocks_later_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def fail(command, **kwargs):
                kwargs["stdout"].write(json.dumps({"type": "turn.failed"}) + "\n")
                return SimpleNamespace(returncode=1)

            with patch.object(runtime.subprocess, "run", side_effect=fail) as invoke:
                with self.assertRaisesRegex(runtime.StudyBlocked, "Codex failed"):
                    runtime.model_call(self.protocol, out, "first", self.schema)
                with self.assertRaisesRegex(runtime.StudyBlocked, "usage is unknown"):
                    runtime.model_call(self.protocol, out, "second", self.schema)
            self.assertEqual(invoke.call_count, 1)
            self.assertTrue(runtime.jsonl(out / "usage.jsonl")[0]["usage_unknown"])

    def test_success_without_usage_is_blocked_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def run(command, **kwargs):
                answer = Path(command[command.index("--output-last-message") + 1])
                answer.write_text('{"answer": "unaccounted"}')
                kwargs["stdout"].write('{"type": "turn.completed"}\n')
                return SimpleNamespace(returncode=0)

            with patch.object(runtime.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(runtime.StudyBlocked, "did not report usage"):
                    runtime.model_call(self.protocol, out, "prompt", self.schema)
            row = runtime.jsonl(out / "usage.jsonl")[0]
            self.assertFalse(row["ok"])
            self.assertTrue(row["usage_unknown"])

    def test_failed_call_with_reported_usage_counts_toward_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)

            def fail(command, **kwargs):
                events = [
                    {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}},
                    {"type": "error", "message": "failure"},
                ]
                kwargs["stdout"].write("\n".join(json.dumps(event) for event in events) + "\n")
                return SimpleNamespace(returncode=1)

            protocol = {**self.protocol, "max_calls_per_condition": 1}
            with patch.object(runtime.subprocess, "run", side_effect=fail) as invoke:
                with self.assertRaises(runtime.StudyBlocked):
                    runtime.model_call(protocol, out, "first", self.schema)
                with self.assertRaisesRegex(runtime.StudyBlocked, "call limit"):
                    runtime.model_call(protocol, out, "second", self.schema)
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(runtime.jsonl(out / "usage.jsonl")[0]["observed_tokens"], 10)

    def test_completed_interrupted_call_is_recovered_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            work = out / "workspace"
            work.mkdir(parents=True)
            identity = runtime.request_identity(self.protocol, "prompt", self.schema, False, [], work)
            calls = out / "calls"
            calls.mkdir()
            runtime.write_json(calls / f"{identity}.started.json", {
                "request_sha256": identity,
                "started_at": 1,
                "ok": False,
                "usage_unknown": True,
            })
            runtime.write_json(calls / f"{identity}.answer.json", {"answer": "recovered"})
            (calls / f"{identity}.events.jsonl").write_text(json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 11, "output_tokens": 4},
            }) + "\n")
            with patch.object(runtime.subprocess, "run") as invoke:
                value = runtime.model_call(self.protocol, out, "prompt", self.schema)
            invoke.assert_not_called()
            self.assertEqual(value, {"answer": "recovered"})
            receipt = runtime.read_json(calls / f"{identity}.json")
            self.assertTrue(receipt["recovered_after_interruption"])
            self.assertEqual(receipt["observed_tokens"], 15)

    def test_interrupted_call_with_reconnect_errors_recovers_completed_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            work = out / "workspace"
            work.mkdir(parents=True)
            identity = runtime.request_identity(self.protocol, "prompt", self.schema, False, [], work)
            calls = out / "calls"
            calls.mkdir()
            runtime.write_json(calls / f"{identity}.started.json", {
                "request_sha256": identity,
                "started_at": 1,
                "ok": False,
                "usage_unknown": True,
            })
            runtime.write_json(calls / f"{identity}.answer.json", {"answer": "recovered"})
            events = [
                {"type": "error", "message": "Reconnecting... 5/5"},
                {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "completed"}},
                {"type": "item.completed", "item": {"type": "agent_message"}},
                {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 4}},
            ]
            (calls / f"{identity}.events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )
            with patch.object(runtime.subprocess, "run") as invoke:
                value = runtime.model_call(self.protocol, out, "prompt", self.schema)
            invoke.assert_not_called()
            self.assertEqual(value, {"answer": "recovered"})
            receipt = runtime.read_json(calls / f"{identity}.json")
            self.assertTrue(receipt["recovered_after_interruption"])
            self.assertEqual(receipt["observed_tokens"], 15)

    def test_unresolved_interrupted_call_blocks_different_request(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            calls = out / "calls"
            calls.mkdir(parents=True)
            runtime.write_json(calls / "old.started.json", {"request_sha256": "old"})
            with patch.object(runtime.subprocess, "run") as invoke:
                with self.assertRaisesRegex(runtime.StudyBlocked, "earlier interrupted"):
                    runtime.model_call(self.protocol, out, "new", self.schema)
            invoke.assert_not_called()

    def test_receipt_without_ledger_row_is_reconciled_before_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            calls = out / "calls"
            calls.mkdir(parents=True)
            identity = "a" * 64
            runtime.write_json(calls / f"{identity}.json", {
                "request_sha256": identity,
                "ok": True,
                "value": {"answer": "old"},
                "observed_tokens": 12,
                "usage_unknown": False,
            })
            protocol = {**self.protocol, "max_calls_per_condition": 1}
            with patch.object(runtime.subprocess, "run") as invoke:
                with self.assertRaisesRegex(runtime.StudyBlocked, "call limit"):
                    runtime.model_call(protocol, out, "new", self.schema)
            invoke.assert_not_called()
            self.assertEqual(runtime.jsonl(out / "usage.jsonl")[0]["observed_tokens"], 12)

    def test_cache_identity_includes_tool_config_source_and_image_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            paper = root / "paper"
            work.mkdir()
            paper.mkdir()
            (paper / "manifest.json").write_text('{"version": 1}')
            config = {"paper": str(paper), "output": str(root / "notes"), "tool_limit": 2}
            runtime.write_json(work / "tool-config.json", config)
            image_a = root / "a.png"
            image_b = root / "b.png"
            image_a.write_bytes(b"same")
            image_b.write_bytes(b"same")
            first = runtime.request_identity(self.protocol, "p", self.schema, True, [image_a], work)
            second = runtime.request_identity(self.protocol, "p", self.schema, True, [image_b], work)
            self.assertNotEqual(first, second)
            (paper / "manifest.json").write_text('{"version": 2}')
            third = runtime.request_identity(self.protocol, "p", self.schema, True, [image_a], work)
            self.assertNotEqual(first, third)
            runtime.write_json(work / "tool-config.json", {**config, "tool_limit": 3})
            fourth = runtime.request_identity(self.protocol, "p", self.schema, True, [image_a], work)
            self.assertNotEqual(third, fourth)

    def test_tool_call_keeps_read_only_sandbox_and_explicit_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            paper = out / "paper"
            paper.mkdir(parents=True)
            (paper / "manifest.json").write_text("{}")
            runtime.write_json(out / "workspace" / "tool-config.json", {"paper": str(paper)})
            seen = []

            def run(command, **kwargs):
                seen.extend(command)
                return self.successful_run()(command, **kwargs)

            with patch.object(runtime.subprocess, "run", side_effect=run):
                runtime.model_call(self.protocol, out, "prompt", self.schema, tools=True)
            self.assertIn("read-only", seen)
            self.assertNotIn("danger-full-access", seen)
            joined = "\n".join(seen)
            for name in runtime.APPROVED_MANUSCRIPT_TOOLS:
                self.assertIn(f"tools.{name}.approval_mode", joined)

    def test_invalid_caps_and_effort_are_rejected_before_inference(self):
        variants = [
            {**self.protocol, "max_calls_per_condition": 0},
            {**self.protocol, "observed_token_stop": True},
            {**self.protocol, "effort": "exhaustive"},
            {**self.protocol, "max_attempts": 0},
            {**self.protocol, "max_attempts": 3},
            {**self.protocol, "max_attempts": "two"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runtime.subprocess, "run") as invoke:
                for protocol in variants:
                    with self.assertRaises(runtime.StudyBlocked):
                        runtime.model_call(protocol, directory, "prompt", self.schema)
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
