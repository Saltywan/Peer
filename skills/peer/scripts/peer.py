#!/usr/bin/env python3
"""Small, local bridge between the Codex, Claude Code, and Antigravity CLIs."""

import argparse
import hashlib
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
CONFIG_KEYS = {"timeout_seconds", "output_dir", "providers"}
PROVIDER_KEYS = {"model", "effort", "efforts", "work_allowed_tools"}
DENIAL = re.compile(
    r"soft[- ]denied|permission denied|operation not permitted|read[- ]only (?:database|file system)|permission[^\n]*(?:denied|not granted|required)|"
    r"approval[^\n]*(?:denied|required|unavailable|cannot)|"
    r"(?:tool|command)[^\n]*(?:denied|not allowed|requires approval)",
    re.IGNORECASE,
)
DEFAULT_TIMEOUT = 600
STOP_GRACE_SECONDS = 5
RELAY_LIMIT = 12000


class Terminated(Exception):
    """Raised in place of the default exit when Peer receives SIGTERM or SIGHUP."""


def raise_terminated(signum, frame):
    raise Terminated(signal.Signals(signum).name)


def install_signal_handlers():
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), raise_terminated)


def ignore_signals():
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            except ValueError:
                pass


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
    for key in config:
        if key not in CONFIG_KEYS:
            raise ValueError("unknown configuration key: {}".format(key))
    output_dir = config.get("output_dir")
    if output_dir is not None and (not isinstance(output_dir, str) or not output_dir.strip()):
        raise ValueError("output_dir must be a nonempty string")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be a JSON object")
    for provider, provider_config in providers.items():
        if provider not in PROVIDERS:
            raise ValueError("unknown provider in configuration: {}".format(provider))
        if not isinstance(provider_config, dict):
            raise ValueError("providers.{} must be a JSON object".format(provider))
        for key in provider_config:
            if key not in PROVIDER_KEYS or (key == "work_allowed_tools" and provider != "claude"):
                raise ValueError("unknown configuration key: providers.{}.{}".format(provider, key))
    return config, path


def effort_levels(provider_config, provider):
    levels = provider_config.get("efforts")
    if levels is None:
        return EFFORTS[provider]
    if not isinstance(levels, list) or not levels or any(not isinstance(x, str) or not x for x in levels):
        raise ValueError("providers.{}.efforts must be a nonempty list of strings".format(provider))
    return set(levels)


def preferences(config, provider, model=None, effort=None, timeout=None):
    provider_config = config.get("providers", {}).get(provider, {})
    chosen_model = model if model is not None else provider_config.get("model")
    chosen_effort = effort if effort is not None else provider_config.get("effort")
    chosen_timeout = timeout if timeout is not None else config.get("timeout_seconds", DEFAULT_TIMEOUT)
    levels = effort_levels(provider_config, provider)
    if chosen_model is not None and (not isinstance(chosen_model, str) or not chosen_model.strip()):
        raise ValueError("{} model must be a nonempty string".format(provider))
    if chosen_effort is not None and chosen_effort not in levels:
        raise ValueError("{} effort must be one of: {}".format(provider, ", ".join(sorted(levels))))
    if isinstance(chosen_timeout, bool) or not isinstance(chosen_timeout, (int, float)) or chosen_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    allowed_tools = provider_config.get("work_allowed_tools", [])
    if not isinstance(allowed_tools, list) or any(not isinstance(x, str) or not x for x in allowed_tools):
        raise ValueError("providers.{}.work_allowed_tools must be a list of strings".format(provider))
    return chosen_model, chosen_effort, chosen_timeout, allowed_tools


def output_dir(config):
    if config.get("output_dir"):
        return Path(config["output_dir"]).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))
    return base / "peer" / "runs"


def output_path(args, config):
    if args.no_save:
        return None
    if args.output:
        return Path(args.output).expanduser().resolve()
    label = args.to if args.command == "ask" else "{}-{}".format(args.a, args.b)
    name = "{}-{}-{}-{}.json".format(time.strftime("%Y%m%d-%H%M%S"), args.command, label, os.getpid())
    return output_dir(config) / name


def save_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(str(tmp), str(path))


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
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        if session:
            command.extend(["--resume", session])
        if mode == "work" and allowed_tools:
            # Variadic flag last, one rule per argument, so rules may contain commas.
            command.append("--allowedTools")
            command.extend(allowed_tools)
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


def json_lines(stdout):
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_codex(stdout):
    session, answer, error = None, None, None
    for event in json_lines(stdout):
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
    for event in json_lines(stdout):
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


def matches(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value)
    return text[:500] if DENIAL.search(text) else None


def permission_signals(provider, mode, stdout, stderr, response):
    """Return (notices, warnings).

    Notices come from structured denials, failed commands, and stderr; they mark the call
    blocked. Warnings are denial-like text in successful tool output or in the reply, such
    as a grep hit or a sentence about a fixed permission error; they do not change status.
    """
    notices = [line.strip() for line in stderr.splitlines() if DENIAL.search(line)]
    warnings = []
    if provider == "claude":
        try:
            data = json.loads(stdout)
        except ValueError:
            data = {}
        for denial in (data.get("permission_denials") or []) if isinstance(data, dict) else []:
            notices.append(str(denial))
    elif provider == "codex":
        for event in json_lines(stdout):
            item = event.get("item") or {}
            if event.get("type") in ("turn.failed", "error"):
                found = matches(event.get("error") or event.get("message"))
                if found:
                    notices.append(found)
            elif item.get("type") == "error":
                found = matches(item.get("message"))
                if found:
                    notices.append(found)
            elif item.get("type") == "command_execution":
                found = matches(item.get("aggregated_output"))
                failed = item.get("status") in ("failed", "declined") or item.get("exit_code") not in (None, 0)
                if found:
                    (notices if failed else warnings).append(found)
    else:
        for event in json_lines(stdout):
            if event.get("event") == "result":
                result = event.get("result") or {}
                for denial in result.get("denied_actions") or []:
                    notices.append("Antigravity denied action: {}".format(denial))
            update = event.get("step_update") or {}
            if update.get("step_type") in ("user_input", "agent_response"):
                continue
            found = matches(update.get("error"))
            if found:
                notices.append(found)
            for candidate in (update.get("message"), update.get("output")):
                found = matches(candidate)
                if found:
                    warnings.append(found)
    if mode == "work":
        found = matches(response)
        if found:
            warnings.append(found)
    notices = list(dict.fromkeys(notices))
    warnings = [w for w in dict.fromkeys(warnings) if w not in notices]
    return notices, warnings


def git_output(cwd, *args):
    try:
        result = subprocess.run(
            ["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def worktree_snapshot(cwd):
    """Git status lines plus a fingerprint of every uncommitted byte, or None outside Git."""
    status = git_output(cwd, "status", "--porcelain", "--untracked-files=all")
    if status is None:
        return None
    digest = hashlib.sha256()
    for args in (("diff",), ("diff", "--cached")):
        diff = git_output(cwd, *args, "--binary", "--no-color", "--no-ext-diff", "--no-textconv")
        if diff is None:
            return None
        digest.update(diff)
    untracked = git_output(cwd, "ls-files", "--others", "--exclude-standard", "-z") or b""
    for name in sorted(p for p in untracked.split(b"\0") if p):
        path = Path(cwd) / os.fsdecode(name)
        digest.update(name + b"\0")
        try:
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(str(path))))
            elif path.is_file():
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    lines = status.decode("utf-8", errors="replace").splitlines()
    return {"status": lines, "fingerprint": digest.hexdigest()}


def stop_process_group(proc):
    """Ask the child's process group to exit, then kill whatever remains."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        proc.kill()


def run_process(command, payload, cwd, timeout):
    env = os.environ.copy()
    env["PEER_BRIDGE_DEPTH"] = str(int(env.get("PEER_BRIDGE_DEPTH", "0")) + 1)
    start = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    stopped = None
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        stopped = "timeout"
    except KeyboardInterrupt:
        stopped = "SIGINT"
    except Terminated as exc:
        stopped = str(exc)
    if stopped:
        if stopped != "timeout":
            # Peer is exiting; a second signal must not skip stopping the child.
            ignore_signals()
        stop_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=STOP_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            stdout, stderr = "", ""
    return proc.returncode, stdout or "", stderr or "", time.monotonic() - start, stopped


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
    before = worktree_snapshot(cwd) if mode == "work" else None
    try:
        exit_code, stdout, stderr, duration, stopped = run_process(command, payload, cwd, timeout)
    except OSError as exc:
        raise ValueError("cannot launch {}: {}".format(provider, exc))
    parser = {"codex": parse_codex, "claude": parse_claude, "agy": parse_agy}[provider]
    parsed_session, response, parse_error = parser(stdout)
    notices, warnings = permission_signals(provider, mode, stdout, stderr, response)
    error = parse_error
    if stopped == "timeout":
        error = "timed out after {} seconds; the child was stopped".format(timeout)
    elif stopped:
        error = "Peer received {}; the child was stopped".format(stopped)
    elif exit_code != 0 and not error:
        error = "{} exited with code {}".format(provider, exit_code)
    elif not (parsed_session or session) and not error:
        error = "{} returned no session ID; the conversation cannot be resumed".format(provider)
    elif response is None and not error and not notices:
        error = "{} returned no response".format(provider)
    if stopped == "timeout":
        status = "timeout"
    elif stopped:
        status = "interrupted"
    else:
        status = "blocked" if notices else "error" if error else "ok"
    if status == "blocked" and not (parsed_session or session):
        error = "{} was blocked before returning a session ID".format(provider)
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
        "permission_warnings": warnings,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 2),
    }
    if stderr.strip():
        result["diagnostics"] = stderr.strip()[-4000:]
    if mode == "work":
        after = worktree_snapshot(cwd)
        result["worktree_status_before"] = before["status"] if before else None
        result["worktree_status_after"] = after["status"] if after else None
        result["worktree_changed"] = (before["fingerprint"] != after["fingerprint"]
                                      if before and after else None)
    return result


def relay_text(response):
    if len(response) <= RELAY_LIMIT:
        return response
    return response[:RELAY_LIMIT] + "\n[truncated: first {} of {} characters shown]".format(
        RELAY_LIMIT, len(response))


def debate(args, cwd, prompt, config, path):
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
                return {"status": result["status"], "sessions": sessions,
                        "transcript": transcript, "error": result.get("error")}
            if path is not None:
                # Keep finished turns on disk in case the host stops this long command.
                try:
                    save_result(path, {"status": "running", "prompt": prompt,
                                       "sessions": sessions, "transcript": transcript})
                except OSError:
                    pass
            relay = relay_text(result["response"])
    return {"status": "ok", "sessions": sessions, "transcript": transcript}


def read_prompt(explicit):
    if explicit is not None:
        return explicit
    if sys.stdin.isatty():
        raise ValueError("pass --prompt or pipe a prompt on stdin")
    return sys.stdin.read()


def emit(result, path=None, prompt=None):
    if path is not None:
        result["output_file"] = str(path)
        record = dict(result, prompt=prompt) if prompt is not None else result
        try:
            save_result(path, record)
        except OSError as exc:
            result["output_file"] = None
            result["output_error"] = "cannot save result to {}: {}".format(path, exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


def add_output_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output", help="save the JSON result to this file")
    group.add_argument("--no-save", action="store_true", help="print the result without saving a file")


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
    add_output_arguments(ask_parser)

    debate_parser = sub.add_parser("debate", help="alternate two consult sessions for bounded rounds")
    debate_parser.add_argument("--a", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--b", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--rounds", type=int, default=2)
    debate_parser.add_argument("--session-a")
    debate_parser.add_argument("--session-b")
    debate_parser.add_argument("--cwd")
    debate_parser.add_argument("--config")
    debate_parser.add_argument("--timeout", type=float)
    debate_parser.add_argument("--prompt")
    add_output_arguments(debate_parser)

    doctor = sub.add_parser("doctor", help="show CLI availability and configured defaults")
    doctor.add_argument("--config")

    args = parser.parse_args(argv)
    path, prompt = None, None
    try:
        config, config_file = load_config(args.config)
        if args.command == "doctor":
            providers = {}
            for provider in PROVIDERS:
                model, effort, timeout, allowed = preferences(config, provider)
                providers[provider] = {
                    "executable": shutil.which(provider), "model": model, "effort": effort,
                    "efforts": sorted(effort_levels(config.get("providers", {}).get(provider, {}), provider)),
                    "timeout_seconds": timeout,
                    "work_allowed_tools": allowed if provider == "claude" else None,
                }
            return emit({"status": "ok", "config_path": str(config_file),
                         "output_dir": str(output_dir(config)), "providers": providers,
                         "note": "Effective host and target permissions require live probes."})
        cwd = normalize_cwd(args.cwd)
        prompt = read_prompt(args.prompt)
        path = output_path(args, config)
        if path is not None:
            print("peer: saving result to {}".format(path), file=sys.stderr, flush=True)
        if args.command == "ask":
            result = ask(args.to, args.mode, cwd, prompt, args.session, args.model,
                         args.effort, args.timeout, config)
        else:
            result = debate(args, cwd, prompt, config, path)
        return emit(result, path, prompt)
    except (ValueError, KeyboardInterrupt, Terminated) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "interrupted"
        return emit({"status": "error", "error": message or "interrupted"}, path, prompt)


if __name__ == "__main__":
    install_signal_handlers()
    sys.exit(main())
