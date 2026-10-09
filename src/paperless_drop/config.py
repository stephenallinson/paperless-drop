"""Configuration loading and token resolution."""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

APP = "paperless-drop"
KEYRING_SERVICE = "paperless-drop"


class ConfigError(Exception):
    """The configuration is missing or invalid. The message is shown to the user."""


def config_dir(env: Mapping[str, str] = os.environ) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def default_config_path(env: Mapping[str, str] = os.environ) -> Path:
    return config_dir(env) / "config.toml"


@dataclass
class Config:
    url: str
    token: str = field(repr=False)
    token_source: str = ""
    tags: list[str] = field(default_factory=list)
    wait: bool = True
    notify: bool = True
    inbox_path: Path = field(default_factory=lambda: Path.home() / "Paperless-Inbox")
    poll_seconds: float = 3.0
    settle_seconds: float = 3.0
    retention_days: int = 30
    config_path: Path | None = None


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def _read_token_file(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"Cannot read token file {path}: {exc.strerror}") from exc
    if mode & 0o077:
        raise ConfigError(f"Token file {path} is readable by others. Run: chmod 600 {path}")
    return path.read_text().strip()


def _read_keyring() -> str | None:
    if not shutil.which("secret-tool"):
        return None
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "service", KEYRING_SERVICE],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def resolve_token(raw: Mapping, env: Mapping[str, str], cfg_dir: Path) -> tuple[str, str]:
    """Return (token, source). Order: environment, token file, Secret Service."""
    if env.get("PAPERLESS_DROP_TOKEN"):
        return env["PAPERLESS_DROP_TOKEN"].strip(), "environment ($PAPERLESS_DROP_TOKEN)"

    explicit = raw.get("token_file")
    token_file = _expand(explicit) if explicit else cfg_dir / "token"
    if explicit or token_file.exists():
        return _read_token_file(token_file), f"file {token_file}"

    token = _read_keyring()
    if token:
        return token, "Secret Service (secret-tool)"

    raise ConfigError(
        "No API token found. Run `paperless-drop init`, or set $PAPERLESS_DROP_TOKEN, "
        f"or put the token in {token_file} (chmod 600)."
    )


def load(path: Path | None = None, env: Mapping[str, str] = os.environ,
         require_token: bool = True) -> Config:
    cfg_path = path or default_config_path(env)
    raw: dict = {}
    if cfg_path.exists():
        try:
            raw = tomllib.loads(cfg_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot parse {cfg_path}: {exc}") from exc
    elif path is not None:
        raise ConfigError(f"Config file {cfg_path} does not exist")

    url = (env.get("PAPERLESS_DROP_URL") or raw.get("url") or "").strip().rstrip("/")
    if not url:
        raise ConfigError(f"No Paperless URL configured. Run `paperless-drop init` (config: {cfg_path}).")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"url must start with http:// or https:// (got {url!r})")

    token, source = "", ""
    if require_token:
        token, source = resolve_token(raw, env, cfg_path.parent)

    inbox = raw.get("inbox", {})
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ConfigError("tags must be a list of tag names")

    return Config(
        url=url,
        token=token,
        token_source=source,
        tags=tags,
        wait=bool(raw.get("wait", True)),
        notify=bool(raw.get("notify", True)),
        inbox_path=_expand(inbox.get("path", "~/Paperless-Inbox")),
        poll_seconds=float(inbox.get("poll_seconds", 3)),
        settle_seconds=float(inbox.get("settle_seconds", 3)),
        retention_days=int(inbox.get("retention_days", 30)),
        config_path=cfg_path,
    )
