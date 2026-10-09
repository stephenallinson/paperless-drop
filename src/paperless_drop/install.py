"""Install / remove the desktop integrations: Open With, Dolphin, Thunar, systemd watcher."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from .sender import SUPPORTED_EXTENSIONS

NAME = "Send to Paperless"
THUNAR_ID = "paperless-drop-0001"
SERVICE = "paperless-drop.service"
SCRIPT_MARK = "# paperless-drop: managed by `paperless-drop install`"
COSMIC_BEGIN = "// paperless-drop: begin (managed; edits between these markers are overwritten)"
COSMIC_END = "// paperless-drop: end"
_COSMIC_BLOCK_RE = re.compile(r"[ \t]*// paperless-drop: begin.*?// paperless-drop: end[ \t]*\n?", re.S)


class InstallError(Exception):
    """An integration could not be installed safely. The message says what to do instead."""

# MIME types the Dolphin menu applies to. Do NOT put these in the Open With entry: declaring a
# MIME type makes the app a candidate default handler and it can displace the user's viewer.
MIME_TYPES = ["application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp",
              "image/gif", "text/plain"]


def find_exe() -> list[str]:
    exe = shutil.which("paperless-drop")
    if exe:
        return [exe]
    return [sys.executable, "-m", "paperless_drop"]


def _desktop_quote(arg: str) -> str:
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return '"' + escaped.replace("\\", "\\\\") + '"'


class Installer:
    def __init__(self, home: Path | None = None, exe: list[str] | None = None,
                 run: Callable = subprocess.run, say: Callable[[str], None] = print):
        self.home = Path(home) if home else Path.home()
        self.exe = exe or find_exe()
        self._run = run
        self.say = say

    # -- paths -----------------------------------------------------------------------------
    @property
    def openwith_path(self) -> Path:
        return self.home / ".local/share/applications/paperless-drop.desktop"

    @property
    def dolphin_path(self) -> Path:
        return self.home / ".local/share/kio/servicemenus/paperless-drop.desktop"

    @property
    def thunar_path(self) -> Path:
        return self.home / ".config/Thunar/uca.xml"

    @property
    def nautilus_path(self) -> Path:
        return self.home / ".local/share/nautilus/scripts" / NAME

    @property
    def cosmic_path(self) -> Path:
        return self.home / ".config/cosmic/com.system76.CosmicFiles/v1/context_actions"

    @property
    def service_path(self) -> Path:
        return self.home / ".config/systemd/user" / SERVICE

    def _exec(self, *extra: str) -> str:
        return " ".join(_desktop_quote(a) for a in [*self.exe, *extra])

    def _write(self, path: Path, text: str, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def _systemctl(self, *args: str) -> bool:
        try:
            proc = self._run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    # -- Open With ---------------------------------------------------------------------------
    def install_openwith(self) -> None:
        text = (
            "[Desktop Entry]\nType=Application\n"
            f"Name={NAME}\nComment=Upload to Paperless-ngx\nIcon=document-send\n"
            f"Exec={self._exec('send', '%F')}\nTerminal=false\nCategories=Utility;\n"
        )
        self._write(self.openwith_path, text)
        self._refresh_desktop_database()
        self.say(f"Open With: {self.openwith_path} (choose it via Open With → Other Application…)")

    def _refresh_desktop_database(self) -> None:
        if shutil.which("update-desktop-database"):
            self._run(["update-desktop-database", str(self.openwith_path.parent)],
                      capture_output=True, timeout=30)

    # -- Dolphin -------------------------------------------------------------------------------
    def install_dolphin(self) -> None:
        text = (
            "[Desktop Entry]\nType=Service\nActions=sendToPaperless;\n"
            f"MimeType={';'.join(MIME_TYPES)};\nX-KDE-Priority=TopLevel\n\n"
            "[Desktop Action sendToPaperless]\n"
            f"Name={NAME}\nIcon=document-send\nExec={self._exec('send', '%F')}\n"
        )
        self._write(self.dolphin_path, text, mode=0o755)  # Plasma 6 requires it to be executable
        self.say(f"Dolphin: {self.dolphin_path}")

    # -- Thunar --------------------------------------------------------------------------------
    def _thunar_action(self) -> ET.Element:
        action = ET.Element("action")
        patterns = sorted({f"*{e}" for e in SUPPORTED_EXTENSIONS} | {f"*{e.upper()}" for e in SUPPORTED_EXTENSIONS})
        for tag, text in (
            ("icon", "document-send"), ("name", NAME), ("submenu", ""), ("unique-id", THUNAR_ID),
            ("command", self._exec("send", "%F")), ("description", "Upload to Paperless-ngx"),
            ("range", "*"), ("patterns", ";".join(patterns)),
        ):
            ET.SubElement(action, tag).text = text
        for tag in ("image-files", "other-files", "text-files"):
            ET.SubElement(action, tag)
        return action

    def _thunar_load(self) -> ET.ElementTree:
        if self.thunar_path.exists():
            return ET.parse(self.thunar_path)
        return ET.ElementTree(ET.Element("actions"))

    def _thunar_save(self, tree: ET.ElementTree) -> None:
        ET.indent(tree, space="\t")
        self.thunar_path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(self.thunar_path, encoding="UTF-8", xml_declaration=True)

    def _thunar_strip(self, root: ET.Element) -> bool:
        removed = False
        for action in list(root.findall("action")):
            if action.findtext("unique-id") == THUNAR_ID:
                root.remove(action)
                removed = True
        return removed

    def install_thunar(self) -> None:
        backup = self.thunar_path.with_name("uca.xml.paperless-drop.bak")
        if self.thunar_path.exists() and not backup.exists():
            shutil.copy2(self.thunar_path, backup)
        tree = self._thunar_load()
        root = tree.getroot()
        self._thunar_strip(root)
        root.append(self._thunar_action())
        self._thunar_save(tree)
        self.say(f"Thunar: {self.thunar_path} (restart Thunar to load it: `thunar -q`)")

    # -- Nautilus (GNOME Files) ------------------------------------------------------------------
    def install_nautilus(self) -> None:
        if self.nautilus_path.exists() and SCRIPT_MARK not in self.nautilus_path.read_text():
            raise InstallError(f"{self.nautilus_path} exists and was not created by paperless-drop; "
                               "not overwriting it")
        text = (f"#!/bin/sh\n{SCRIPT_MARK}\n"
                '[ "$#" -gt 0 ] || exit 0   # right-click on the background selects no files\n'
                f'exec {shlex.join(self.exe)} send "$@"\n')
        self._write(self.nautilus_path, text, mode=0o755)
        self.say(f"Nautilus: {self.nautilus_path} (right-click → Scripts → {NAME})")

    # -- COSMIC Files ----------------------------------------------------------------------------
    def _cosmic_block(self) -> str:
        # Double quotes, not shlex's single quotes: valid for both shlex and .desktop Exec parsing.
        step = json.dumps(self._exec("send", "%F"), ensure_ascii=False)
        name = json.dumps(NAME)
        return (f"    {COSMIC_BEGIN}\n    (\n        name: {name},\n        confirm: false,\n"
                f"        selection: Files,\n        steps: [\n            {step},\n        ],\n"
                f"    ),\n    {COSMIC_END}\n")

    def _cosmic_merge(self, old: str, block: str) -> str:
        """Add our block to a RON list of actions without touching the user's own entries."""
        text = _COSMIC_BLOCK_RE.sub("", old)
        end = text.rfind("]")
        if end == -1 or "[" not in text[:end]:
            raise InstallError(f"{self.cosmic_path} is not a list I recognise; add the entry yourself:\n"
                               f"{block}")
        head = text[:end].rstrip()
        if head.splitlines()[-1].strip().startswith("//"):
            raise InstallError(f"{self.cosmic_path} ends in a comment, so I can't safely add an entry; "
                               f"add it before the closing ]:\n{block}")
        if not head.endswith(("[", ",")):
            head += ","
        return head + "\n" + block + text[end:]

    def install_cosmic(self) -> None:
        path = self.cosmic_path
        block = self._cosmic_block()
        if path.exists():
            # cosmic-config reads every file inside v1/ as a setting, so the backup goes outside it.
            backup = path.parent.parent / "context_actions.paperless-drop.bak"
            if not backup.exists():
                shutil.copy2(path, backup)
            text = self._cosmic_merge(path.read_text(), block)
        else:
            text = f"[\n{block}]\n"
        self._write(path, text)
        self.say(f"COSMIC Files: {path} (restart it: `pkill -x cosmic-files`)")

    # -- systemd watcher -------------------------------------------------------------------------
    def install_watcher(self) -> None:
        text = (
            "[Unit]\nDescription=paperless-drop inbox watcher\n"
            "After=network-online.target graphical-session.target\n\n"
            f"[Service]\nExecStart={self._exec('watch')}\nRestart=on-failure\nRestartSec=5\n"
            "RestartPreventExitStatus=2\n\n"  # 2 = configuration error: restarting cannot fix it
            "[Install]\nWantedBy=default.target\n"
        )
        self._write(self.service_path, text)
        ok = self._systemctl("daemon-reload") and self._systemctl("enable", "--now", SERVICE)
        self.say(f"Watcher: {self.service_path} " + ("(enabled and started)" if ok else
                 "(written; enable it with: systemctl --user enable --now paperless-drop)"))

    # -- removal -----------------------------------------------------------------------------------
    def uninstall(self) -> None:
        if self.service_path.exists():
            self._systemctl("disable", "--now", SERVICE)
        for path in (self.service_path, self.dolphin_path, self.openwith_path):
            if path.exists():
                path.unlink()
                self.say(f"Removed {path}")
        if self.thunar_path.exists():
            tree = self._thunar_load()
            if self._thunar_strip(tree.getroot()):
                self._thunar_save(tree)
                self.say(f"Removed the Thunar action from {self.thunar_path}")
        if self.nautilus_path.exists() and SCRIPT_MARK in self.nautilus_path.read_text():
            self.nautilus_path.unlink()
            self.say(f"Removed {self.nautilus_path}")
        if self.cosmic_path.exists():
            old = self.cosmic_path.read_text()
            new = _COSMIC_BLOCK_RE.sub("", old)
            if new != old:
                if re.sub(r"\s+", "", new) == "[]":  # nothing of the user's left
                    self.cosmic_path.unlink()
                else:
                    self.cosmic_path.write_text(new)
                self.say(f"Removed the COSMIC Files action from {self.cosmic_path}")
        self._systemctl("daemon-reload")
        self._refresh_desktop_database()

    def status(self) -> dict[str, bool]:
        thunar = False
        if self.thunar_path.exists():
            try:
                thunar = any(a.findtext("unique-id") == THUNAR_ID
                             for a in ET.parse(self.thunar_path).getroot().findall("action"))
            except ET.ParseError:
                pass
        def has_mark(path: Path, mark: str) -> bool:
            try:
                return mark in path.read_text()
            except OSError:
                return False

        return {"openwith": self.openwith_path.exists(), "dolphin": self.dolphin_path.exists(),
                "thunar": thunar, "nautilus": has_mark(self.nautilus_path, SCRIPT_MARK),
                "cosmic": has_mark(self.cosmic_path, COSMIC_BEGIN), "watcher": self.service_path.exists()}
