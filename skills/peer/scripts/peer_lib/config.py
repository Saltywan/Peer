"""Configuration loading, validation, preferences, atomic file writes, and model listing."""

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from peer_lib.constants import (
    CONFIG_KEYS,
    DEFAULT_REPLY_WORDS,
    DEFAULT_TIMEOUT,
    EFFORTS,
    PROVIDER_KEYS,
    PROVIDERS,
)

__all__ = [
    "config_path", "load_config", "provider_settings", "effort_levels",
    "preferences", "validate_timeout", "validate_words", "state_dir",
    "output_dir", "save_result", "model_listing",
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
    return {"model": chosen_model, "model_alias": alias, "effort": chosen_effort,
            "timeout": chosen_timeout, "allowed_tools": allowed_tools}


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
