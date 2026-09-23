import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "peer" / "scripts" / "peer.py"
spec = importlib.util.spec_from_file_location("peer", SCRIPT)
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


FAKE_CLI = """#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
payload = sys.stdin.read()
pathlib.Path(os.environ['FAKE_LOG']).write_text(json.dumps({'name': name, 'args': sys.argv[1:], 'stdin': payload}))
if os.environ.get('FAKE_EDIT') == '1':
    pathlib.Path('edited.txt').write_text('changed')
if os.environ.get('FAKE_DENY') == '1':
    print('Tool run_command requires approval and was soft-denied', file=sys.stderr)
if os.environ.get('FAKE_HOST_DENY') == '1':
    print('open state_5.sqlite: operation not permitted', file=sys.stderr)
    raise SystemExit(1)
if name == 'codex':
    if os.environ.get('FAKE_NO_SESSION') != '1':
        print(json.dumps({'type': 'thread.started', 'thread_id': 'codex-id'}))
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'Permission denied by sandbox'}}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'codex reply'}}))
elif name == 'claude':
    result = {'session_id': 'claude-id', 'result': 'claude reply', 'is_error': False}
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        result['permission_denials'] = [{'tool': 'Bash', 'reason': 'approval required'}]
    print(json.dumps(result))
else:
    print(json.dumps({'event': 'init', 'conversation_id': 'agy-id'}))
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        print(json.dumps({'event': 'step_update', 'step_update': {'step_type': 'tool_call', 'error': 'permission denied'}}))
    result = {'conversation_id': 'agy-id', 'status': 'SUCCESS', 'response': 'agy reply'}
    if os.environ.get('FAKE_RESULT_DENY') == '1':
        result['denied_actions'] = [{'action': 'command', 'display_name': 'RunCommand'}]
    print(json.dumps({'event': 'result', 'result': result}))
"""


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in peer.PROVIDERS:
            cli = self.bin / name
            cli.write_text(FAKE_CLI)
            cli.chmod(0o755)
        self.log = root / "call.json"
        self.cwd = root / "project"
        self.cwd.mkdir()
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"providers": {"claude": {
            "work_allowed_tools": ["Bash(git diff *)"]}}}))
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log), "PEER_BRIDGE_DEPTH": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = peer.main(list(args) + ["--config", str(self.config)])
        return code, json.loads(output.getvalue())

    def logged(self):
        return json.loads(self.log.read_text())

    def test_fresh_and_explicit_resume_for_each_provider(self):
        for provider in peer.PROVIDERS:
            with self.subTest(provider=provider):
                code, first = self.call("ask", "--to", provider, "--cwd", str(self.cwd),
                                        "--prompt", "First question")
                self.assertEqual(code, 0)
                self.assertEqual(first["status"], "ok")
                self.assertEqual(first["session_id"], provider + "-id")
                code, resumed = self.call("ask", "--to", provider, "--cwd", str(self.cwd),
                                          "--session", first["session_id"], "--prompt", "Follow up")
                self.assertEqual(code, 0)
                self.assertEqual(resumed["session_id"], first["session_id"])
                command = self.logged()["args"]
                self.assertIn(first["session_id"], command)
                self.assertNotIn("--ephemeral", command)
                self.assertNotIn("--no-session-persistence", command)

    def test_work_mode_passes_permissions_and_reports_git_status(self):
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--cwd", str(self.cwd), "--model", "gpt-6-sol",
                                     "--effort", "high", "--prompt", "Edit a file")
        self.assertEqual(code, 0)
        self.assertIn("?? edited.txt", result["worktree_status_after"])
        command = self.logged()["args"]
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("gpt-6-sol", command)
        self.assertIn('model_reasoning_effort="high"', command)
        code, result = self.call("ask", "--to", "claude", "--mode", "work",
                                 "--cwd", str(self.cwd), "--prompt", "Edit a file")
        command = self.logged()["args"]
        self.assertIn("acceptEdits", command)
        self.assertIn("Bash(git diff *)", command)
        code, result = self.call("ask", "--to", "agy", "--mode", "work",
                                 "--cwd", str(self.cwd), "--prompt", "Edit a file")
        command = self.logged()["args"]
        self.assertIn("accept-edits", command)
        self.assertIn("--sandbox", command)

    def test_agy_soft_denial_is_blocked_even_with_zero_exit(self):
        with mock.patch.dict(os.environ, {"FAKE_DENY": "1"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "work",
                                     "--cwd", str(self.cwd), "--prompt", "Run tests")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["session_id"], "agy-id")
        self.assertTrue(result["permission_notices"])

    def test_agy_result_denied_actions_are_blocked(self):
        with mock.patch.dict(os.environ, {"FAKE_RESULT_DENY": "1"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("RunCommand", result["permission_notices"][0])

    def test_host_filesystem_denial_is_blocked(self):
        with mock.patch.dict(os.environ, {"FAKE_HOST_DENY": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["error"], "codex was blocked before returning a session ID")
        self.assertTrue(result["permission_notices"])

    def test_structured_denials_are_blocked_for_every_provider(self):
        for provider in peer.PROVIDERS:
            with self.subTest(provider=provider):
                with mock.patch.dict(os.environ, {"FAKE_STRUCTURED_DENY": "1"}):
                    code, result = self.call("ask", "--to", provider, "--mode", "work",
                                             "--cwd", str(self.cwd), "--prompt", "Run a command")
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["permission_notices"])

    def test_success_without_session_id_is_error(self):
        with mock.patch.dict(os.environ, {"FAKE_NO_SESSION": "1"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                     "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("no session ID", result["error"])

    def test_codex_resume_uses_config_override_not_fresh_only_flags(self):
        self.call("ask", "--to", "codex", "--mode", "consult", "--cwd", str(self.cwd),
                  "--session", "codex-id", "--prompt", "Review")
        command = self.logged()["args"]
        self.assertIn("resume", command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertNotIn("-C", command)
        self.assertNotIn("--sandbox", command)

    def test_invalid_effort_fails_before_launch(self):
        code, result = self.call("ask", "--to", "agy", "--effort", "max",
                                 "--cwd", str(self.cwd), "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("agy effort", result["error"])
        self.assertFalse(self.log.exists())

    def test_bounded_debate_retains_both_ids(self):
        code, result = self.call("debate", "--a", "codex", "--b", "claude",
                                 "--rounds", "2", "--cwd", str(self.cwd), "--prompt", "Tradeoffs?")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["transcript"]), 4)
        self.assertEqual(result["sessions"], {"codex": "codex-id", "claude": "claude-id"})
        self.assertTrue(all(turn["mode"] == "consult" for turn in result["transcript"]))

    def test_nested_peer_call_is_rejected(self):
        with mock.patch.dict(os.environ, {"PEER_BRIDGE_DEPTH": "1"}):
            code, result = self.call("ask", "--to", "claude", "--cwd", str(self.cwd),
                                     "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("nested Peer calls", result["error"])


if __name__ == "__main__":
    unittest.main()
