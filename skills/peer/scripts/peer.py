#!/usr/bin/env python3
"""Small, local bridge between the Codex, Claude Code, and Antigravity CLIs."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


PROVIDERS = ("codex", "claude", "agy")
EFFORTS = {
    "codex": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "agy": {"low", "medium", "high"},
}
DENIAL = re.compile(
    r"soft[- ]denied|permission denied|permission[^\n]*(?:denied|not granted|required)|"
    r"approval[^\n]*(?:denied|required|unavailable|cannot)|"
    r"(?:tool|command)[^\n]*(?:denied|not allowed|requires approval)",
    re.IGNORECASE,
)
DEFAULT_TIMEOUT = 600


def config_path(explicit):
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("PEER_CONFIG"):
        return Path(os.environ["PEER_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "peer" / "config.json"


def load_config(explicit):
    path = config_path(explicit)
    if not path.exists():
        if explicit or os.environ.get("PEER_CONFIG"):
            raise ValueError("configuration file does not exist: {}".format(path))
        return {}, path
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read configuration {}: {}".format(path, exc))
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be a JSON object")
    return config, path


def preferences(config, provider, model=None, effort=None, timeout=None):
    provider_config = config.get("providers", {}).get(provider, {})
    if not isinstance(provider_config, dict):
        raise ValueError("providers.{} must be a JSON object".format(provider))
    chosen_model = model if model is not None else provider_config.get("model")
    chosen_effort = effort if effort is not None else provider_config.get("effort")
    chosen_timeout = timeout if timeout is not None else config.get("timeout_seconds", DEFAULT_TIMEOUT)
    if chosen_model is not None and (not isinstance(chosen_model, str) or not chosen_model.strip()):
        raise ValueError("{} model must be a nonempty string".format(provider))
    if chosen_effort is not None and chosen_effort not in EFFORTS[provider]:
        raise ValueError("{} effort must be one of: {}".format(provider, ", ".join(sorted(EFFORTS[provider]))))
    if isinstance(chosen_timeout, bool) or not isinstance(chosen_timeout, (int, float)) or chosen_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    allowed_tools = provider_config.get("work_allowed_tools", [])
    if not isinstance(allowed_tools, list) or any(not isinstance(x, str) or not x for x in allowed_tools):
        raise ValueError("providers.{}.work_allowed_tools must be a list of strings".format(provider))
    return chosen_model, chosen_effort, chosen_timeout, allowed_tools


def normalize_cwd(value):
    path = Path(value or os.getcwd()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("working directory does not exist: {}".format(path))
    return str(path)


def prompt_for_mode(prompt, mode, cwd):
    if mode == "consult":
        lead = ("Peer consultation in {}: analyze and answer. Do not edit files or run "
                "mutating commands.").format(cwd)
    else:
        lead = ("Peer delegated work in {}: you may edit files only in this workspace. "
                "Use absolute paths under this directory with file-edit tools; shell commands "
                "may need separate permission. Report what changed and any blocked actions.").format(cwd)
    return lead + "\nDo not invoke the Peer bridge from within this call.\n\n" + prompt


def build_command(provider, mode, cwd, session, model, effort, allowed_tools, prompt):
    if provider == "codex":
        command = ["codex", "exec"]
        if session:
            command.append("resume")
        command.extend([
            "--json", "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="{}"'.format("read-only" if mode == "consult" else "workspace-write"),
        ])
        if model:
            command.extend(["-m", model])
        if effort:
            command.extend(["-c", 'model_reasoning_effort="{}"'.format(effort)])
        if session:
            command.extend([session, "-"])
        else:
            command.extend(["-C", cwd, "-"])
        return command, prompt
    if provider == "claude":
        command = ["claude", "-p", "--output-format", "json", "--permission-mode",
                   "plan" if mode == "consult" else "acceptEdits"]
        if mode == "consult":
            command.extend(["--tools", "Read,Glob,Grep"])
        elif allowed_tools:
            command.extend(["--allowedTools", ",".join(allowed_tools)])
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        if session:
            command.extend(["--resume", session])
        return command, prompt
    command = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
               "--mode", "plan" if mode == "consult" else "accept-edits", "--sandbox"]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if session:
        command.extend(["--conversation", session])
    event = {"event": "user", "message": {"content": prompt}}
    return command, json.dumps(event, ensure_ascii=False) + "\n"


def parse_codex(stdout):
    session, answer, error = None, None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session = event.get("thread_id") or session
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = item.get("text")
        elif kind in ("turn.failed", "error"):
            error = str(event.get("error") or event.get("message") or kind)
    return session, answer, error


def parse_claude(stdout):
    try:
        result = json.loads(stdout)
    except ValueError as exc:
        return None, None, "invalid Claude JSON: {}".format(exc)
    if not isinstance(result, dict):
        return None, None, "Claude result is not an object"
    error = str(result.get("result") or "Claude reported an error") if result.get("is_error") else None
    return result.get("session_id"), result.get("result"), error


def parse_agy(stdout):
    session, answer, error, status = None, None, None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "init":
            session = event.get("conversation_id") or session
        elif event.get("event") == "result":
            result = event.get("result") or {}
            session = result.get("conversation_id") or session
            answer = result.get("response")
            status = result.get("status")
            if status != "SUCCESS":
                error = str(result.get("error") or "Antigravity status: {}".format(status))
    if status is None:
        error = "Antigravity did not return a result event"
    return session, answer, error


def permission_notices(stderr):
    return [line.strip() for line in stderr.splitlines() if DENIAL.search(line)]


def structured_permission_notices(provider, stdout, response, mode):
    notices = []
    if provider == "claude":
        try:
            data = json.loads(stdout)
        except ValueError:
            data = {}
        for denial in data.get("permission_denials", []) if isinstance(data, dict) else []:
            notices.append(str(denial))
        if mode == "work" and isinstance(response, str) and DENIAL.search(response):
            notices.append(response[:500])
    else:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if provider == "codex":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    continue
                candidates = (item.get("aggregated_output"), item.get("error"), event.get("message"))
            else:
                if event.get("event") == "result":
                    result = event.get("result") or {}
                    for denial in result.get("denied_actions", []):
                        notices.append("Antigravity denied action: {}".format(denial))
                update = event.get("step_update") or {}
                if update.get("step_type") in ("user_input", "agent_response"):
                    continue
                candidates = (update.get("error"), update.get("message"), update.get("output"))
            for candidate in candidates:
                if candidate is not None:
                    value = candidate if isinstance(candidate, str) else json.dumps(candidate)
                    if DENIAL.search(value):
                        notices.append(value[:500])
    return notices


def worktree_status(cwd):
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"], cwd=cwd,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.splitlines() if result.returncode == 0 else None


def run_process(command, payload, cwd, timeout):
    env = os.environ.copy()
    env["PEER_BRIDGE_DEPTH"] = str(int(env.get("PEER_BRIDGE_DEPTH", "0")) + 1)
    start = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
        return proc.returncode, stdout, stderr, time.monotonic() - start, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr, time.monotonic() - start, True


def ask(provider, mode, cwd, prompt, session, model, effort, timeout, config):
    if int(os.environ.get("PEER_BRIDGE_DEPTH", "0")) >= 1:
        raise ValueError("nested Peer calls are disabled; use the outer caller to coordinate turns")
    if not prompt.strip():
        raise ValueError("prompt is empty")
    model, effort, timeout, allowed_tools = preferences(config, provider, model, effort, timeout)
    if not shutil.which(provider):
        raise ValueError("{} CLI is not installed or not on PATH".format(provider))
    prepared_prompt = prompt_for_mode(prompt, mode, cwd)
    command, payload = build_command(provider, mode, cwd, session, model, effort, allowed_tools, prepared_prompt)
    before = worktree_status(cwd) if mode == "work" else None
    try:
        exit_code, stdout, stderr, duration, timed_out = run_process(command, payload, cwd, timeout)
    except OSError as exc:
        raise ValueError("cannot launch {}: {}".format(provider, exc))
    parser = {"codex": parse_codex, "claude": parse_claude, "agy": parse_agy}[provider]
    parsed_session, response, parse_error = parser(stdout)
    notices = permission_notices(stderr)
    notices.extend(structured_permission_notices(provider, stdout, response, mode))
    notices = list(dict.fromkeys(notices))
    error = parse_error
    if timed_out:
        error = "timed out after {} seconds".format(timeout)
    elif exit_code != 0 and not error:
        error = "{} exited with code {}".format(provider, exit_code)
    elif not (parsed_session or session) and not error:
        error = "{} returned no session ID; the conversation cannot be resumed".format(provider)
    elif response is None and not error and not notices:
        error = "{} returned no response".format(provider)
    status = "timeout" if timed_out else "error" if error else "blocked" if notices else "ok"
    result = {
        "status": status,
        "provider": provider,
        "mode": mode,
        "cwd": cwd,
        "session_id": parsed_session or session,
        "model": model,
        "effort": effort,
        "response": response or "",
        "error": error,
        "permission_notices": notices,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 2),
    }
    if stderr.strip():
        result["diagnostics"] = stderr.strip()[-4000:]
    if mode == "work":
        result["worktree_status_before"] = before
        result["worktree_status_after"] = worktree_status(cwd)
    return result


def read_prompt(explicit):
    if explicit is not None:
        return explicit
    if sys.stdin.isatty():
        raise ValueError("pass --prompt or pipe a prompt on stdin")
    return sys.stdin.read()


def emit(result):
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consult or delegate work to a local agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    ask_parser = sub.add_parser("ask", help="send one turn, optionally resuming a session")
    ask_parser.add_argument("--to", required=True, choices=PROVIDERS)
    ask_parser.add_argument("--mode", choices=("consult", "work"), default="consult")
    ask_parser.add_argument("--cwd")
    ask_parser.add_argument("--session")
    ask_parser.add_argument("--model")
    ask_parser.add_argument("--effort")
    ask_parser.add_argument("--timeout", type=float)
    ask_parser.add_argument("--config")
    ask_parser.add_argument("--prompt")

    debate = sub.add_parser("debate", help="alternate two consult sessions for bounded rounds")
    debate.add_argument("--a", required=True, choices=PROVIDERS)
    debate.add_argument("--b", required=True, choices=PROVIDERS)
    debate.add_argument("--rounds", type=int, default=2)
    debate.add_argument("--session-a")
    debate.add_argument("--session-b")
    debate.add_argument("--cwd")
    debate.add_argument("--config")
    debate.add_argument("--timeout", type=float)
    debate.add_argument("--prompt")

    doctor = sub.add_parser("doctor", help="show CLI availability and configured defaults")
    doctor.add_argument("--config")

    args = parser.parse_args(argv)
    try:
        config, path = load_config(args.config)
        if args.command == "doctor":
            providers = {}
            for provider in PROVIDERS:
                model, effort, timeout, allowed = preferences(config, provider)
                providers[provider] = {
                    "executable": shutil.which(provider), "model": model, "effort": effort,
                    "timeout_seconds": timeout,
                    "work_allowed_tools": allowed if provider == "claude" else None,
                }
            return emit({"status": "ok", "config_path": str(path), "providers": providers,
                         "note": "Effective host and target permissions require live probes."})
        cwd = normalize_cwd(args.cwd)
        prompt = read_prompt(args.prompt)
        if args.command == "ask":
            return emit(ask(args.to, args.mode, cwd, prompt, args.session, args.model,
                            args.effort, args.timeout, config))
        if args.a == args.b:
            raise ValueError("debate requires two different providers")
        if not 1 <= args.rounds <= 10:
            raise ValueError("rounds must be between 1 and 10")
        transcript = []
        sessions = {args.a: args.session_a, args.b: args.session_b}
        relay = prompt
        for _ in range(args.rounds):
            for provider in (args.a, args.b):
                message = prompt if not transcript else (
                    "Original question:\n{}\n\n{} replied:\n{}\n\nRespond to their reasoning."
                    .format(prompt, transcript[-1]["provider"], relay)
                )
                result = ask(provider, "consult", cwd, message, sessions[provider],
                             None, None, args.timeout, config)
                sessions[provider] = result["session_id"]
                transcript.append(result)
                if result["status"] != "ok":
                    return emit({"status": result["status"], "sessions": sessions,
                                 "transcript": transcript, "error": result.get("error")})
                relay = result["response"][:12000]
        return emit({"status": "ok", "sessions": sessions, "transcript": transcript})
    except (ValueError, KeyboardInterrupt) as exc:
        return emit({"status": "error", "error": str(exc) or "interrupted"})


if __name__ == "__main__":
    sys.exit(main())
