"""Configuration loading, validation, preferences, atomic file writes, and model listing."""

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from peer_lib.constants import (
    CONFIG_KEYS,
    DEFAULT_REPLY_WORDS,
    DEFAULT_TIMEOUT,
    EFFORTS,
    MODES,
    PROVIDER_KEYS,
    PROVIDERS,
    WAIT_SECONDS,
    JsonArgumentParser,
)

__all__ = [
    "config_path", "load_config", "provider_settings", "effort_levels",
    "preferences", "validate_timeout", "validate_words", "state_dir",
    "output_dir", "save_result", "model_listing",
    "read_prompt", "add_view_arguments", "add_run_arguments", "build_parser",
]


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
    out_dir = config.get("output_dir")
    if out_dir is not None and (not isinstance(out_dir, str) or not out_dir.strip()):
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
        aliases = provider_config.get("aliases", {})
        if not isinstance(aliases, dict) or any(
                not k.strip() or not isinstance(v, str) or not v.strip() for k, v in aliases.items()):
            raise ValueError("providers.{}.aliases must map names to model strings".format(provider))
        if len({k.lower() for k in aliases}) != len(aliases):
            raise ValueError("providers.{}.aliases must have distinct case-insensitive names".format(provider))
    validate_timeout(config.get("timeout_seconds", DEFAULT_TIMEOUT))
    validate_words(config.get("reply_words", DEFAULT_REPLY_WORDS), "reply_words")
    for provider in PROVIDERS:
        preferences(config, provider)
    return config, path


def provider_settings(config, provider):
    return config.get("providers", {}).get(provider, {})


def effort_levels(provider_config, provider):
    levels = provider_config.get("efforts")
    if levels is None:
        return EFFORTS[provider]
    if not isinstance(levels, list) or not levels or any(not isinstance(x, str) or not x for x in levels):
        raise ValueError("providers.{}.efforts must be a nonempty list of strings".format(provider))
    return set(levels)


def preferences(config, provider, model=None, effort=None, timeout=None):
    provider_config = provider_settings(config, provider)
    chosen_model = model if model is not None else provider_config.get("model")
    chosen_effort = effort if effort is not None else provider_config.get("effort")
    chosen_timeout = timeout if timeout is not None else config.get("timeout_seconds", DEFAULT_TIMEOUT)
    levels = effort_levels(provider_config, provider)
    if chosen_model is not None and (not isinstance(chosen_model, str) or not chosen_model.strip()):
        raise ValueError("{} model must be a nonempty string".format(provider))
    alias = None
    aliases = {k.lower(): v for k, v in provider_config.get("aliases", {}).items()}
    if chosen_model is not None and chosen_model.lower() in aliases:
        alias, chosen_model = chosen_model, aliases[chosen_model.lower()]
    if chosen_effort is not None and (not isinstance(chosen_effort, str) or chosen_effort not in levels):
        raise ValueError("{} effort must be one of: {}".format(provider, ", ".join(sorted(levels))))
    validate_timeout(chosen_timeout)
    allowed_tools = provider_config.get("work_allowed_tools", [])
    if not isinstance(allowed_tools, list) or any(not isinstance(x, str) or not x for x in allowed_tools):
        raise ValueError("providers.{}.work_allowed_tools must be a list of strings".format(provider))
    extra_args = provider_config.get("extra_args", [])
    if not isinstance(extra_args, list) or any(not isinstance(x, str) or not x for x in extra_args):
        raise ValueError("providers.{}.extra_args must be a list of strings".format(provider))
    return {"model": chosen_model, "model_alias": alias, "effort": chosen_effort,
            "timeout": chosen_timeout, "allowed_tools": allowed_tools, "extra_args": list(extra_args)}


def validate_timeout(value):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float)) and
                 value > 0 and math.isfinite(value))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("timeout_seconds must be a finite positive number")


def validate_words(value, label="--words"):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise ValueError("{} must be a whole number from 0 to 10000 (0 turns the reply format off)".format(label))
    return value


def state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))
    return base / "peer"


def output_dir(config):
    if config.get("output_dir"):
        return Path(config["output_dir"]).expanduser()
    return state_dir() / "runs"


def save_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(str(tmp), str(path))


def model_listing(config):
    listing = {}
    for provider in PROVIDERS:
        settings = provider_settings(config, provider)
        entry = {"configured_model": settings.get("model"), "aliases": settings.get("aliases", {})}
        if provider == "agy" and shutil.which("agy"):
            try:
                result = subprocess.run(["agy", "models"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, timeout=60)
                entry["live"] = [line.split("\t")[0] for line in result.stdout.splitlines() if "\t" in line]
            except (OSError, subprocess.TimeoutExpired) as exc:
                entry["live_error"] = str(exc)
        elif provider == "agy":
            entry["live_error"] = "agy CLI is not installed or not on PATH"
        else:
            entry["check"] = ("{} has no command that lists models; ask the user, who can open /model "
                              "in an interactive session.").format("Codex" if provider == "codex" else "Claude Code")
        listing[provider] = entry
    return listing


def read_prompt(explicit):
    if explicit is not None:
        return explicit
    if sys.stdin.isatty():
        raise ValueError("pass --prompt or pipe a prompt on stdin")
    return sys.stdin.read()


def add_view_arguments(parser):
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--full", action="store_true",
                        help="print everything, including details, warnings, and full diagnostics")


def add_run_arguments(parser):
    parser.add_argument("--cwd")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--config")
    parser.add_argument("--prompt")
    parser.add_argument("--words", type=int, metavar="N",
                        help="ask for an answer of at most N words plus optional details (0 turns this off)")
    parser.add_argument("--attach", action="append", metavar="FILE",
                        help="file to give the target; images go to Codex natively, others by path")
    add_view_arguments(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output", help="save the result record to this file")
    group.add_argument("--no-save", action="store_true", help="print the full result without saving a file")


def build_parser():
    parser = JsonArgumentParser(description="Consult or delegate work to a local agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    ask_parser = sub.add_parser("ask", help="send one turn to a provider, optionally starting a named session")
    ask_parser.add_argument("--to", choices=PROVIDERS, help="target provider (inferred for --session-name/--fork-from)")
    ask_parser.add_argument("--mode", choices=MODES)
    ask_parser.add_argument("--model")
    ask_parser.add_argument("--effort")
    ask_parser.add_argument("--extra-arg", action="append", metavar="ARG",
                            help="extra flag or argument for the target CLI (repeatable; or pass after --)")
    ask_parser.add_argument("--new-session", metavar="NAME", help="save the session under NAME")
    ask_parser.add_argument("--worktree", nargs="?", const=True, metavar="NAME",
                            help="run in an isolated Git worktree (default name: --new-session or a timestamp)")
    resume = ask_parser.add_mutually_exclusive_group()
    resume.add_argument("--session", help="resume this native session ID")
    resume.add_argument("--session-name", help="resume the session saved under this name")
    resume.add_argument("--fork-from", metavar="NAME", help="fork the session saved under NAME into a new session")
    resume.add_argument("--fork-session", metavar="ID", help="fork this native session ID into a new session")
    add_run_arguments(ask_parser)

    reply_parser = sub.add_parser("reply", help="continue a named session with its saved settings")
    reply_parser.add_argument("name")
    reply_parser.add_argument("--to", choices=PROVIDERS, help="pick the provider when the name has several")
    reply_parser.add_argument("--mode", choices=MODES)
    reply_parser.add_argument("--model")
    reply_parser.add_argument("--effort")
    reply_parser.add_argument("--extra-arg", action="append", metavar="ARG",
                              help="extra flag or argument for the target CLI (repeatable; or pass after --)")
    add_run_arguments(reply_parser)

    debate_parser = sub.add_parser("debate", help="alternate two consult sessions for bounded rounds")
    debate_parser.add_argument("--a", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--b", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--rounds", type=int, default=2)
    for side in ("a", "b"):
        debate_parser.add_argument("--session-" + side)
        debate_parser.add_argument("--model-" + side)
        debate_parser.add_argument("--effort-" + side)
        debate_parser.add_argument("--extra-arg-" + side, action="append", metavar="ARG")
    names = debate_parser.add_mutually_exclusive_group()
    names.add_argument("--session-name", help="resume both sessions saved under this name")
    names.add_argument("--new-session", metavar="NAME", help="start both sessions and save them under NAME")
    add_run_arguments(debate_parser)

    for name, text in (("show", "print a saved result, its details, or recent turns"),
                       ("wait", "wait for a run to finish, then print it")):
        view = sub.add_parser(name, help=text)
        view.add_argument("target", nargs="?", help="session name or run file (default: latest run here)")
        view.add_argument("--to", choices=PROVIDERS, help="pick the provider when the name has several")
        view.add_argument("--cwd")
        view.add_argument("--config")
        add_view_arguments(view)
        if name == "show":
            view.add_argument("--details", action="store_true", help="print the details part of the reply")
            view.add_argument("--last", type=int, metavar="N", help="print the last N runs of the session")
        else:
            view.add_argument("--timeout", type=float, default=WAIT_SECONDS,
                              help="seconds to wait before reporting that it is still running")

    sessions_parser = sub.add_parser("sessions", help="list recorded sessions and their names")
    sessions_parser.add_argument("--cwd")
    sessions_parser.add_argument("--all", action="store_true", help="include every directory")
    sessions_parser.add_argument("--config")
    sessions_parser.add_argument("--json", action="store_true", help="print JSON instead of text")

    for name, text in (("doctor", "show CLI availability and configured defaults"),
                       ("models", "show configured models and aliases, and list Antigravity models live")):
        diagnostic = sub.add_parser(name, help=text + " (always JSON)")
        diagnostic.add_argument("--config")
        diagnostic.add_argument("--json", action="store_true", help="accepted for consistency; output is JSON")
    return parser
