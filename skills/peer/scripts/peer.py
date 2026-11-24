#!/usr/bin/env python3
"""Small, local bridge between the Codex, Claude Code, and Antigravity CLIs."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from peer_lib.config import *  # noqa: F401, F403, E402
from peer_lib.constants import *  # noqa: F401, F403, E402
from peer_lib.providers import *  # noqa: F401, F403, E402
from peer_lib.render import *  # noqa: F401, F403, E402
from peer_lib.storage import *  # noqa: F401, F403, E402
from peer_lib.workspace import *  # noqa: F401, F403, E402

register_peer(globals())


def ask(provider, mode, cwd, prompt, config, session=None, model=None, effort=None,
        timeout=None, attachments=(), progress=None, words=0, debate=False, fork=False, extra_args=()):
    if int(os.environ.get("PEER_BRIDGE_DEPTH", "0")) >= 1:
        raise ValueError("nested Peer calls are disabled; use the outer caller to coordinate turns")
    if not prompt.strip():
        raise ValueError("prompt is empty")
    prefs = preferences(config, provider, model, effort, timeout)
    if not shutil.which(provider):
        raise ValueError("{} CLI is not installed or not on PATH".format(provider))
    effective_extra = list(prefs["extra_args"]) + list(extra_args or ())
    prepared_prompt = prompt_for_mode(prompt, mode, cwd, attachments)
    if words:
        prepared_prompt += reply_contract(words, mode, debate)
    command, payload = build_command(provider, mode, cwd, session, prefs["model"], prefs["effort"],
                                     prefs["allowed_tools"], prepared_prompt, attachments,
                                     fork=fork, extra_args=effective_extra)
    parser = PARSERS[provider]
    before = worktree_snapshot(cwd) if mode in WORK_MODES else None
    effective_session = None if fork else session

    def on_progress(elapsed, lines):
        if progress:
            progress({"elapsed_seconds": round(elapsed), "output_events": len(lines),
                      "session_id": parser("".join(lines))[0] or effective_session,
                      "last_activity": last_activity(provider, lines)})

    try:
        exit_code, stdout, stderr, duration, stopped = run_process(
            command, payload, cwd, prefs["timeout"], on_progress)
    except OSError as exc:
        raise ValueError("cannot launch {}: {}".format(provider, exc))
    parsed_session, response, parse_error = parser(stdout)
    notices, warnings = permission_signals(provider, mode, stdout, stderr, response)
    error = parse_error
    if stopped == "timeout":
        error = "timed out after {} seconds; the child was stopped".format(prefs["timeout"])
    elif stopped:
        error = "Peer received {}; the child was stopped".format(stopped)
    elif exit_code != 0 and not error:
        error = "{} exited with code {}".format(provider, exit_code)
    elif not (parsed_session or effective_session) and not error:
        error = "{} returned no session ID; the conversation cannot be resumed".format(provider)
    elif response is None and not error and not notices:
        error = "{} returned no response".format(provider)
    if stopped == "timeout":
        status = "timeout"
    elif stopped:
        status = "interrupted"
    else:
        status = "blocked" if notices else "error" if error else "ok"
    if status == "blocked" and not (parsed_session or effective_session):
        error = "{} was blocked before returning a session ID".format(provider)
    answer, details, questions = split_reply(response, words)
    result = {
        "status": status,
        "provider": provider,
        "mode": mode,
        "cwd": cwd,
        "session_id": parsed_session or effective_session,
        "model": prefs["model"],
        "model_alias": prefs["model_alias"],
        "effort": prefs["effort"],
        "response": answer,
        "response_chars": len(answer),
        "response_words": len(answer.split()),
        "questions": questions,
        "details": details,
        "details_chars": len(details),
        "reply_words": words,
        "error": error,
        "permission_notices": notices,
        "permission_warnings": warnings,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 2),
    }
    if fork and session:
        result["forked_from"] = session
    if effective_extra:
        result["extra_args"] = effective_extra
    if attachments:
        result["attachments"] = [{"path": a, "native": native_attachment(provider, a)} for a in attachments]
    if stderr.strip():
        result["diagnostics"] = stderr.strip()[-4000:]
    if mode in WORK_MODES:
        result.update(worktree_report(before, worktree_snapshot(cwd)))
    return result


def prepare_debate(args, cwd, prompt, config):
    if int(os.environ.get("PEER_BRIDGE_DEPTH", "0")) >= 1:
        raise ValueError("nested Peer calls are disabled; use the outer caller to coordinate turns")
    if not prompt.strip():
        raise ValueError("prompt is empty")
    if args.a == args.b:
        raise ValueError("debate requires two different providers")
    if not 1 <= args.rounds <= 10:
        raise ValueError("rounds must be between 1 and 10")
    name = args.session_name or args.new_session
    if name and (args.session_a or args.session_b):
        raise ValueError("use either session names or --session-a/--session-b, not both")
    sessions = {args.a: args.session_a, args.b: args.session_b}
    saved = {args.a: {}, args.b: {}}
    origin_cwd = cwd
    if args.session_name:
        matches = {p: resolve_name(args.session_name, cwd, p) for p in (args.a, args.b)}
        if matches[args.a]["cwd"] != matches[args.b]["cwd"]:
            raise ValueError("'{}' names {} and {} sessions in different workspaces".format(
                args.session_name, args.a, args.b))
        wt_a = matches[args.a]["settings"].get("worktree")
        wt_b = matches[args.b]["settings"].get("worktree")
        if wt_a != wt_b:
            raise ValueError("'{}' names {} and {} sessions with different worktrees".format(
                args.session_name, args.a, args.b))
        origin_cwd = matches[args.a]["cwd"]
        if wt_a:
            cwd = verify_worktree(origin_cwd, wt_a, matches[args.a]["settings"].get("worktree_branch"))
        else:
            cwd = origin_cwd
        sessions = {p: m["session_id"] for p, m in matches.items()}
        saved = {p: m["settings"] for p, m in matches.items()}
    elif args.new_session:
        check_name(args.new_session)
    side = {
        args.a: (args.model_a, args.effort_a, list(args.extra_arg_a or ())),
        args.b: (args.model_b, args.effort_b, list(args.extra_arg_b or ())),
    }
    for provider in (args.a, args.b):
        preferences(config, provider, side[provider][0], side[provider][1], args.timeout)
        if not shutil.which(provider):
            raise ValueError("{} CLI is not installed or not on PATH".format(provider))
    return name, sessions, saved, side, cwd, origin_cwd


def debate(args, cwd, prompt, config, run, attachments, words, prepared=None):
    name, sessions, saved, side, cwd, origin_cwd = prepared or prepare_debate(args, cwd, prompt, config)
    transcript = []
    summary = {"sessions": sessions, "transcript": transcript}
    if name:
        summary["session_name"] = name
    run.update(cwd=cwd, origin_cwd=origin_cwd if origin_cwd != cwd else None, **summary)
    relay = prompt
    for _ in range(args.rounds):
        for provider in (args.a, args.b):
            first_turn = all(t["provider"] != provider for t in transcript)
            if not transcript:
                message = prompt
            else:
                exchange = "{} replied:\n{}\n\nRespond to their reasoning.".format(
                    transcript[-1]["provider"], relay)
                # A fresh side needs the brief; later turns already have it in native history.
                message = ("Debate brief:\n{}\n\n{}".format(prompt, exchange)
                           if first_turn else exchange)
            side_extra = side[provider][2] or list(saved[provider].get("extra_args") or ())
            result = ask(provider, "consult", cwd, message, config, session=sessions[provider],
                         model=side[provider][0], effort=side[provider][1], timeout=args.timeout,
                         attachments=attachments if first_turn else (),
                         progress=lambda info, p=provider: run.update(current=dict(info, provider=p)),
                         words=words, debate=True, extra_args=side_extra)
            sessions[provider] = result["session_id"]
            transcript.append(result)
            run.update(current=None, **summary)
            if name and result["session_id"]:
                settings = session_settings(result, extra_args=side_extra)
                for key in ("mode", "worktree", "worktree_branch"):
                    if saved[provider].get(key):
                        settings[key] = saved[provider][key]
                bind_name(provider, origin_cwd, name, result["session_id"], settings)
            if result["status"] != "ok":
                return dict(summary, status=result["status"], error=result.get("error"))
            # The other side gets the whole reply; the caller sees only the answers.
            relay = relay_text(full_reply(result))
    return dict(summary, status="ok")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    peer_argv, passthrough = split_passthrough(argv)
    fmt = "json" if "--json" in peer_argv else "text"
    full = "--full" in peer_argv
    run = None
    try:
        args = build_parser().parse_args(peer_argv)
        if passthrough:
            if args.command == "debate":
                raise ValueError("for debate, use --extra-arg-a / --extra-arg-b instead of --")
            if args.command not in ("ask", "reply"):
                raise ValueError("extra CLI arguments after -- are only supported for ask and reply")
        config, config_file = load_config(args.config)
        if args.command == "doctor":
            providers = {}
            for provider in PROVIDERS:
                prefs = preferences(config, provider)
                providers[provider] = {
                    "executable": shutil.which(provider), "model": prefs["model"],
                    "model_alias": prefs["model_alias"], "effort": prefs["effort"],
                    "efforts": sorted(effort_levels(provider_settings(config, provider), provider)),
                    "timeout_seconds": prefs["timeout"],
                    "work_allowed_tools": prefs["allowed_tools"] if provider == "claude" else None,
                    "extra_args": prefs["extra_args"],
                }
            return emit({"status": "ok", "config_path": str(config_file),
                         "output_dir": str(output_dir(config)),
                         "reply_words": config.get("reply_words", DEFAULT_REPLY_WORDS), "providers": providers,
                         "note": "Effective host and target permissions require live probes."}, fmt="json")
        if args.command == "models":
            return emit({"status": "ok", "providers": model_listing(config)}, fmt="json")
        cwd = normalize_cwd(args.cwd)
        if args.command == "sessions":
            result = {"status": "ok", "cwd": None if args.all else cwd,
                      "sessions": recorded_sessions(config, cwd, args.all)}
            print(json.dumps(result, ensure_ascii=False) if fmt == "json" else render_sessions(result, cwd))
            return 0
        if args.command == "show":
            if args.last is not None and args.last < 1:
                raise ValueError("--last must be at least 1")
            data, text = show(args, config, cwd)
            print(json.dumps(data, ensure_ascii=False) if data is not None else text)
            return 0
        if args.command == "wait":
            validate_timeout(args.timeout)
            path, record = wait(args, config, cwd)
            state = run_state(record)
            if fmt == "json":
                shown = record if full or state != "ok" else compact(record)
                print(json.dumps(dict(shown, status=state, output_file=str(path)), ensure_ascii=False))
            else:
                print(render(record, full, path))
            return 0 if state == "ok" else EXIT_RUNNING if state == "running" else 2
        default_words = config.get("reply_words", DEFAULT_REPLY_WORDS)
        if args.words is not None:
            validate_words(args.words)
        prompt = read_prompt(args.prompt)
        attachments = resolve_attachments(args.attach)
        if args.command == "debate":
            words = args.words if args.words is not None else default_words
            prepared = prepare_debate(args, cwd, prompt, config)
            run = RunFile(output_path(args, config, "{}-{}".format(args.a, args.b)), command="debate",
                          providers=[args.a, args.b], cwd=prepared[4],
                          origin_cwd=prepared[5] if prepared[5] != prepared[4] else None,
                          prompt=prompt, session_name=prepared[0])
            return emit(debate(args, cwd, prompt, config, run, attachments, words, prepared), run, fmt, full)
        # ask and reply: a continued named session runs in its own workspace with its saved settings.
        session, name, new_name, settings, fork = None, None, None, {}, False
        origin_cwd = cwd
        worktree_arg = None
        if args.command == "reply":
            match = resolve_name(args.name, cwd, args.to)
            provider, name = match["provider"], args.name
        else:
            if args.new_session and (args.session or args.session_name):
                raise ValueError("--new-session cannot be combined with --session or --session-name")
            if args.new_session:
                check_name(args.new_session)
                new_name = args.new_session
            worktree_arg = args.worktree
            if args.session_name:
                match = resolve_name(args.session_name, cwd, args.to)
                provider, name = match["provider"], args.session_name
            elif args.fork_from:
                match = resolve_name(args.fork_from, cwd, args.to)
                provider, fork = match["provider"], True
            else:
                match = None
                if not args.to:
                    raise ValueError("ask requires --to PROVIDER (unless --session-name or --fork-from is given)")
                provider = args.to
                session = args.fork_session or args.session
                fork = bool(args.fork_session)
        if match:
            origin_cwd, session, settings = match["cwd"], match["session_id"], match["settings"]
        mode = args.mode or settings.get("mode") or "consult"
        model = args.model if args.model is not None else settings.get("model")
        effort = args.effort if args.effort is not None else settings.get("effort")
        cli_extra = list(args.extra_arg or ()) + passthrough
        extra_args = cli_extra if cli_extra else list(settings.get("extra_args") or ())
        if args.words is not None:
            words = args.words
        elif settings.get("words") is not None:
            words = settings["words"]
        else:
            words = default_words
        # Preflight before creating worktrees or run files so invalid calls leave no artifacts.
        if int(os.environ.get("PEER_BRIDGE_DEPTH", "0")) >= 1:
            raise ValueError("nested Peer calls are disabled; use the outer caller to coordinate turns")
        if not prompt.strip():
            raise ValueError("prompt is empty")
        if fork and provider == "agy":
            raise ValueError("agy CLI does not support session forking; use codex or claude")
        preferences(config, provider, model, effort, args.timeout)
        if not shutil.which(provider):
            raise ValueError("{} CLI is not installed or not on PATH".format(provider))
        worktree = settings.get("worktree")
        worktree_branch = settings.get("worktree_branch")
        if worktree_arg:
            wt_name = (worktree_arg if isinstance(worktree_arg, str)
                       else new_name or name or "wt-{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
            worktree, worktree_branch = prepare_worktree(origin_cwd, wt_name)
        elif worktree:
            worktree = verify_worktree(origin_cwd, worktree, worktree_branch)
        effective_cwd = worktree or origin_cwd
        label = name or new_name
        run = RunFile(output_path(args, config, provider), command=args.command, providers=[provider],
                      cwd=effective_cwd, origin_cwd=origin_cwd if origin_cwd != effective_cwd else None,
                      prompt=prompt, session_name=label)
        result = ask(provider, mode, effective_cwd, prompt, config, session=session, model=model, effort=effort,
                     timeout=args.timeout, attachments=attachments, progress=lambda info: run.update(**info),
                     words=words, fork=fork, extra_args=extra_args)
        if worktree:
            result["worktree"] = worktree
            result["worktree_branch"] = worktree_branch
            if origin_cwd != effective_cwd:
                result["origin_cwd"] = origin_cwd
        run.record.update(result)
        if label and result["session_id"]:
            result["session_name"] = label
            previous = bind_name(provider, origin_cwd, label, result["session_id"],
                                 session_settings(result, extra_args=extra_args))
            if previous and new_name:
                result["replaced_session_id"] = previous
        return emit(result, run, fmt, full)
    except (ValueError, OSError, KeyboardInterrupt, Terminated) as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, Terminated))
        result = {key: value for key, value in (run.record.items() if run else [])
                  if key not in ("pid", "status", "prompt", "started_at", "updated_at")}
        current = result.get("current") or {}
        if current.get("session_id") and "sessions" in result:
            result["sessions"][current["provider"]] = current["session_id"]
        result.update(status="interrupted" if interrupted else "error", error=str(exc) or "interrupted")
        return emit(result, run, fmt, full)


if __name__ == "__main__":
    install_signal_handlers()
    sys.exit(main())
