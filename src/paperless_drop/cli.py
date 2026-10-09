"""Command-line entry point."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .api import ApiError, PaperlessClient, ServiceProblem
from .config import APP, KEYRING_SERVICE, Config, ConfigError, config_dir, default_config_path, load
from .inbox import Inbox
from .install import InstallError, Installer
from .notify import Notifier
from .sender import Outcome, Status, send_file
from .watcher import Watcher

ICONS = {Status.ADDED: "✓", Status.DUPLICATE: "=", Status.QUEUED: "…", Status.FAILED: "✗"}


def _client(cfg: Config) -> PaperlessClient:
    return PaperlessClient(cfg.url, cfg.token)


def _describe(o: Outcome) -> str:
    detail = o.message
    if o.document_id:
        detail = f"document #{o.document_id}" + (f" “{o.title}”" if o.title else "")
        if o.status is Status.DUPLICATE:
            detail = f"already in Paperless as {detail}"
    return f"{ICONS[o.status]} {o.path.name}: {detail}"


# -- send ---------------------------------------------------------------------------------------

def cmd_send(args, cfg: Config, notifier: Notifier) -> int:
    if args.title and len(args.files) != 1:
        print("--title can only be used with a single file", file=sys.stderr)
        return 2
    client = _client(cfg)
    inbox = Inbox(cfg.inbox_path, cfg.retention_days, cfg.settle_seconds)
    wait = cfg.wait and not args.no_wait
    outcomes: list[Outcome] = []
    try:
        tag_ids = client.resolve_tags([*cfg.tags, *args.tag])
        for name in args.files:
            path = Path(name).expanduser()
            # A file already sitting in the inbox goes through the same claim step as the
            # watcher, so the two can never upload it twice.
            claimed = inbox.claim(path) if inbox.contains(path) and path.is_file() else None
            try:
                outcome = send_file(client, claimed or path, tag_ids, args.title, wait,
                                    force=args.force)
            except ServiceProblem:
                if claimed:
                    inbox.unclaim(claimed)
                raise
            if claimed:
                inbox.archive(claimed, outcome)
            outcomes.append(outcome)
            if not args.quiet:
                print(_describe(outcome))
    except ServiceProblem as exc:
        print(f"error: {exc}", file=sys.stderr)
        notifier.problem(str(exc))
        if outcomes:
            notifier.summary(outcomes)
        return 2
    notifier.summary(outcomes)
    return 0 if all(o.ok for o in outcomes) else 1


# -- watch --------------------------------------------------------------------------------------

def cmd_watch(args, cfg: Config, notifier: Notifier) -> int:
    inbox = Inbox(cfg.inbox_path, cfg.retention_days, cfg.settle_seconds)
    watcher = Watcher(cfg, _client(cfg), inbox, notifier)
    if args.once:
        inbox.ensure()
        inbox.recover()
        outcomes = watcher.once()
        for o in outcomes:
            print(_describe(o))
        return 0 if all(o.ok for o in outcomes) else 1
    watcher.run()
    return 0


# -- doctor -------------------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    bad = 0

    def row(ok: bool | None, text: str) -> None:
        nonlocal bad
        mark = {True: "✓", False: "✗", None: "-"}[ok]
        bad += ok is False
        print(f" {mark} {text}")

    cfg_path = args.config or default_config_path()
    row(cfg_path.exists() or None, f"config file: {cfg_path}")
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        row(False, str(exc))
        return 1
    row(True, f"server URL: {cfg.url}")
    row(True, f"token from {cfg.token_source}")

    client = _client(cfg)
    try:
        client.check_auth()
        row(True, "token accepted by the server")
        row(True, f"Paperless-ngx version: {client.version or 'unknown (no X-Version header)'}")
        for tag in cfg.tags:
            try:
                row(True, f"tag {tag!r} exists (id {client.find_tag(tag)})")
            except ServiceProblem as exc:
                row(False, str(exc))
    except ServiceProblem as exc:
        row(False, str(exc))

    root = Inbox(cfg.inbox_path).root
    if root.is_dir():
        row(os.access(root, os.W_OK), f"inbox folder: {root}")
    else:
        row(None, f"inbox folder {root} does not exist yet (`paperless-drop install` creates it)")
    row(bool(shutil.which("notify-send")) or None, "notify-send available")
    row(bool(shutil.which("xdg-open")) or None, "xdg-open available")

    status = Installer(say=lambda s: None).status()
    for key, label, binary in (("openwith", "Open With entry", None),
                               ("dolphin", "Dolphin service menu", "dolphin"),
                               ("thunar", "Thunar action", "thunar"),
                               ("nautilus", "Nautilus script", "nautilus"),
                               ("cosmic", "COSMIC Files action", "cosmic-files")):
        if binary and not status[key] and not shutil.which(binary):
            continue  # don't mention file managers that aren't installed
        row(True if status[key] else None, f"{label}: {'installed' if status[key] else 'not installed'}")
    if status["watcher"]:
        try:
            proc = subprocess.run(["systemctl", "--user", "is-active", "paperless-drop.service"],
                                  capture_output=True, text=True, timeout=15)
            state = proc.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            state = "unknown (could not run systemctl)"
        row(state == "active", f"watcher service: {state}")
    else:
        row(None, "watcher service: not installed")
    return 1 if bad else 0


# -- init ---------------------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
url = "{url}"

# Extra tag names added to every upload. Normally empty: Paperless workflows do the tagging.
tags = []
wait = true      # wait for Paperless to finish and report the document id
notify = true

[inbox]
path = "~/Paperless-Inbox"
retention_days = 30    # keep local copies in sent/ this long; 0 = forever
"""


def cmd_init(args) -> int:
    path = args.config or default_config_path()
    existing = ""
    if path.exists():
        try:
            existing = load(path, require_token=False).url
        except ConfigError:
            pass
    url = (args.url or input(f"Paperless URL [{existing or 'https://docs.example.com'}]: ").strip() or existing).rstrip("/")
    if not url.startswith(("http://", "https://")):
        print("error: the URL must start with http:// or https://", file=sys.stderr)
        return 2
    token = os.environ.get("PAPERLESS_DROP_TOKEN") or getpass.getpass(
        "API token (Paperless → My Profile → API Auth Token; input hidden): ").strip()
    if not token:
        print("error: no token entered", file=sys.stderr)
        return 2

    print("Checking the token…")
    try:
        PaperlessClient(url, token).check_auth()
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE.format(url=url))
        print(f"Wrote {path}")
    elif existing != url:
        print(f"Config {path} already exists; set url = \"{url}\" there if needed.")

    if args.keyring:
        proc = subprocess.run(
            ["secret-tool", "store", "--label=paperless-drop", "service", KEYRING_SERVICE],
            input=token, text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"error: could not store the token in the keyring: {proc.stderr.strip()}", file=sys.stderr)
            return 1
        print("Stored the token in the Secret Service keyring.")
    else:
        token_file = path.parent / "token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        print(f"Saved the token to {token_file} (mode 600).")
    print("Done. Next: `paperless-drop doctor`, then `paperless-drop install`.")
    return 0


# -- install / uninstall ------------------------------------------------------------------------

# (flag, program that must exist for a default install, Installer method)
FILE_MANAGERS = (("dolphin", "dolphin", "install_dolphin"), ("thunar", "thunar", "install_thunar"),
                 ("nautilus", "nautilus", "install_nautilus"),
                 ("cosmic", "cosmic-files", "install_cosmic"))


def cmd_install(args) -> int:
    inst = Installer()
    picked = [k for k in ("openwith", "watcher", *(f[0] for f in FILE_MANAGERS)) if getattr(args, k)]
    everything = not picked
    rc = 0
    if everything or "openwith" in picked:
        inst.install_openwith()
    for flag, program, method in FILE_MANAGERS:
        if flag in picked or (everything and shutil.which(program)):
            try:
                getattr(inst, method)()
            except InstallError as exc:
                print(f"warning: {flag} not installed: {exc}", file=sys.stderr)
                rc = 1
        elif everything:
            print(f"{program} not found; skipped (use --{flag} to install anyway)")
    if everything or "watcher" in picked:
        try:
            cfg = load(args.config)  # the watcher needs a working URL and token from the start
        except ConfigError as exc:
            print(f"Watcher NOT installed: {exc}\n"
                  "Run `paperless-drop init`, then `paperless-drop install --watcher`.", file=sys.stderr)
            return 1
        Inbox(cfg.inbox_path).ensure()
        print(f"Inbox: {cfg.inbox_path}")
        inst.install_watcher()
    return rc


def cmd_uninstall(args) -> int:
    Installer().uninstall()
    return 0


# -- main ---------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP, description="Send files to Paperless-ngx.")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("send", help="upload files")
    s.add_argument("files", nargs="+")
    s.add_argument("--tag", action="append", default=[], metavar="NAME", help="extra tag (repeatable)")
    s.add_argument("--title")
    s.add_argument("--no-wait", action="store_true", help="don't wait for Paperless to finish")
    s.add_argument("--force", action="store_true",
                   help="upload even if an identical file is already in Paperless")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")

    w = sub.add_parser("watch", help="watch the inbox folder")
    w.add_argument("--once", action="store_true", help="process the inbox once and exit")

    sub.add_parser("doctor", help="check configuration and connectivity")

    i = sub.add_parser("init", help="create the config and store the API token")
    i.add_argument("--url")
    i.add_argument("--keyring", action="store_true", help="store the token in the Secret Service")

    ins = sub.add_parser("install", help="install right-click menus and the watcher service")
    for flag in ("openwith", "watcher", *(f[0] for f in FILE_MANAGERS)):
        ins.add_argument(f"--{flag}", action="store_true")
    sub.add_parser("uninstall", help="remove everything `install` added")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.command == "init":
        return cmd_init(args)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)

    # Commands that talk to Paperless. Errors here must reach the user even when started from a
    # file manager (no terminal), so they are also sent as desktop notifications.
    quiet_notify = getattr(args, "no_notify", False)
    if args.command == "doctor":
        return cmd_doctor(args)
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        Notifier(enabled=not quiet_notify).problem(str(exc))
        return 2
    notifier = Notifier(cfg.url, cfg.notify and not quiet_notify)
    if args.command == "send":
        return cmd_send(args, cfg, notifier)
    return cmd_watch(args, cfg, notifier)
