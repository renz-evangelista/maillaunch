"""Config file loading, and where secrets come from instead.

The brief's example config shows `gmail.client_secret`, but the paragraph under
it says secrets must not live in the config file. The rule wins: this module
reads client *ids* from YAML and pulls every secret from the environment.
"""

import os
from pathlib import Path

import yaml

HOME = Path(os.environ.get("MAILLAUNCH_HOME", Path.home() / ".maillaunch"))
CONFIG_NAMES = ("maillaunch.config.yaml", "maillaunch.config.yml")

# Google issues a "client secret" even for installed apps, where it is not
# actually secret -- it ships inside the binary. It is still kept out of the
# config file, because the file is the thing that gets committed by accident.
ENV_GMAIL_SECRET = "MAILLAUNCH_GMAIL_CLIENT_SECRET"
ENV_ENCRYPTION_KEY = "MAILLAUNCH_SECRET_KEY"

DEFAULTS = {
    "provider": "gmail",
    "daily_limit": 100,
    "gmail": {"client_id": None},
    "microsoft": {"client_id": None, "tenant": "common"},
    "send": {
        "min_delay_seconds": 60,
        "max_delay_seconds": 180,
        "retry_failed": True,
        "retry_attempts": 2,
        "retry_backoff_seconds": 30,
    },
}


class ConfigError(Exception):
    """Raised when the config is missing, unreadable or internally inconsistent."""


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config(explicit=None):
    """Locate the config: --config, then the cwd, then ~/.maillaunch/."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    for directory in (Path.cwd(), HOME):
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load(explicit=None):
    """Load config, apply defaults, and validate the parts that must be sane."""
    path = find_config(explicit)
    raw = {}
    if path:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level.")

    config = _merge(DEFAULTS, raw)
    config["_path"] = str(path) if path else None

    send = config["send"]
    if send["min_delay_seconds"] > send["max_delay_seconds"]:
        raise ConfigError(
            f"send.min_delay_seconds ({send['min_delay_seconds']}) is greater than "
            f"send.max_delay_seconds ({send['max_delay_seconds']})."
        )
    if send["min_delay_seconds"] < 0:
        raise ConfigError("send.min_delay_seconds cannot be negative.")
    if config["daily_limit"] < 1:
        raise ConfigError("daily_limit must be at least 1.")
    if config["provider"] not in ("gmail", "microsoft"):
        raise ConfigError(
            f"provider must be 'gmail' or 'microsoft', got {config['provider']!r}."
        )
    return config


def oauth_client(config, provider):
    """The client id, and for Gmail the secret, needed to run an OAuth flow."""
    client_id = (config.get(provider) or {}).get("client_id")
    if not client_id:
        raise ConfigError(
            f"No {provider}.client_id in the config file. "
            f"See maillaunch.config.yaml for the expected shape."
        )
    secret = None
    if provider == "gmail":
        secret = os.environ.get(ENV_GMAIL_SECRET)
        if not secret:
            raise ConfigError(
                f"Set {ENV_GMAIL_SECRET} to your Gmail OAuth client secret. "
                "It is kept out of the config file on purpose."
            )
    return client_id, secret
