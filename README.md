# paperless-drop

Get documents from your desktop into [Paperless-ngx](https://docs.paperless-ngx.com/) without
opening the web UI: a CLI, a right-click menu (Thunar, Dolphin, Nautilus, COSMIC Files) and a watched inbox folder.
Standard library only, Python ≥ 3.11, Linux.

- Design and decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Server side (automatic OCR and metadata with paperless-gpt): [docs/SERVER-RUNBOOK.md](docs/SERVER-RUNBOOK.md)

## What you get

| Do this | Result |
|---|---|
| Right-click a file → **Send to Paperless** | Uploaded; a notification says "Added to Paperless" with the document title. Click it to open the document. |
| Save or drop a file into `~/Paperless-Inbox` | The background watcher uploads it within a few seconds |
| `paperless-drop send a.pdf b.pdf` | Same, from a terminal. Exit code 0 = ok, 1 = a file was rejected, 2 = can't reach Paperless |
| Send a file Paperless already has | "Already in Paperless" with a link. Nothing is uploaded. (`--force` overrides.) |

---

## Install on a computer

Everything below is per computer. Repeat it on each machine you use.

### 1. Requirements

- A Linux desktop with a **systemd user session** (for the background watcher) and a
  notification daemon. KDE Plasma and GNOME have one. On a bare window manager (sway, i3, …)
  install `dunst` or `mako`.
- The computer must be able to reach your Paperless server. Here that means **Tailscale is up**.
- An **API token** for your Paperless user (step 4).

Packages (the Python and packaging side is handled by `uv`, which downloads a suitable Python
by itself if the system one is older than 3.11):

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S uv libnotify xdg-utils desktop-file-utils libsecret` |
| Debian / Ubuntu | `sudo apt install libnotify-bin xdg-utils desktop-file-utils libsecret-tools` then install uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Fedora | `sudo dnf install uv libnotify xdg-utils desktop-file-utils libsecret` |

What each is for: `libnotify` provides `notify-send` (notifications), `xdg-utils` provides
`xdg-open` (the notification's Open action), `desktop-file-utils` refreshes the menu cache, and
`libsecret` provides `secret-tool` (only needed if you want the token in the keyring).

Make sure `~/.local/bin` is on your `PATH` (where `uv tool` puts the command):

```fish
uv tool update-shell        # or, in fish:  fish_add_path ~/.local/bin
```
Open a new terminal afterwards.

### 2. Get the code onto the machine

Pick one.

**A. Wheel file (no git needed).** On the machine that has the project:

```fish
cd ~/Projects/paperless-drop
uv build                                   # creates dist/paperless_drop-<version>-py3-none-any.whl
scp dist/*.whl YOU@OTHER-PC:               # or: tailscale file cp dist/*.whl OTHER-PC:
```

**B. Copy the folder.**

```fish
rsync -a --exclude .git ~/Projects/paperless-drop/ YOU@OTHER-PC:~/Projects/paperless-drop/
```

**C. Git.** If you push the project to a private repository:

```fish
git clone <your-repo-url> ~/Projects/paperless-drop
```

### 3. Install the command

On the new machine, from the wheel (A) or the project folder (B, C):

```fish
uv tool install ~/paperless_drop-0.1.0-py3-none-any.whl     # A: path to the wheel you copied
# or
cd ~/Projects/paperless-drop && uv tool install .           # B and C
```

(`pipx install <wheel-or-folder>` also works if you prefer pipx and have Python ≥ 3.11.)

Check it: `paperless-drop --version`

### 4. Connect it to Paperless

In Paperless open **My Profile → API Auth Token** and copy the token. A Paperless user has **one**
token that every computer shares. Copy the existing one, and don't press the regenerate button, or
every other computer stops working.

```fish
paperless-drop init
```

It asks for the Paperless URL (for example `https://docs.example.com`) and the token (hidden as you
type), checks them against the server, and saves:

- `~/.config/paperless-drop/config.toml`
- `~/.config/paperless-drop/token` (permissions 600)

Prefer the desktop keyring over a file? Use `paperless-drop init --keyring`. Note that the
background watcher then needs the keyring unlocked at login. The token file is the simpler,
more reliable choice.

```fish
paperless-drop doctor
```

Everything should show ✓ (a `-` just means "not installed"). If the server line fails, check
that Tailscale is connected and that the URL opens in a browser.

### 5. Install the integrations

```fish
paperless-drop install
```

This sets up, for whatever is present on that machine:

| What | Where it goes |
|---|---|
| **Thunar** right-click action "Send to Paperless" | merged into `~/.config/Thunar/uca.xml`. Your other actions are kept, and a backup is saved as `uca.xml.paperless-drop.bak` |
| **Dolphin** right-click action | `~/.local/share/kio/servicemenus/paperless-drop.desktop` (only if Dolphin is installed) |
| **Nautilus** (GNOME Files) script "Send to Paperless" | `~/.local/share/nautilus/scripts/Send to Paperless`. It appears under **right-click → Scripts**, because Nautilus has no top-level custom actions (only if Nautilus is installed) |
| **COSMIC Files** action "Send to Paperless" | merged into `~/.config/cosmic/com.system76.CosmicFiles/v1/context_actions`, between `paperless-drop` marker comments, so your own actions are kept. A backup is saved as `~/.config/cosmic/com.system76.CosmicFiles/context_actions.paperless-drop.bak`. Shown only when no folder is selected (only if COSMIC Files is installed) |
| An application entry "Send to Paperless" | `~/.local/share/applications/paperless-drop.desktop`. It is deliberately **not** registered for any file type, so it never becomes the default way to open PDFs. Use it via *Open With → Other Application…* |
| The **inbox folder** | `~/Paperless-Inbox` |
| The **background watcher** | a systemd user service, `paperless-drop.service`, enabled and started |

Finally restart your file manager so it loads the new entry:

| File manager | Restart |
|---|---|
| Thunar | `thunar -q` (it relaunches the next time you open a folder) |
| Nautilus | usually not needed; if "Scripts" doesn't show the entry, `nautilus -q` |
| COSMIC Files | `pkill -x cosmic-files` (it reads its actions only at startup) |
| Dolphin | usually not needed; if the entry is missing, log out and in |

Everything stays under your home directory (no `sudo`). Run `paperless-drop init` first: `install`
refuses to start the watcher until the URL and token work. You can install individual pieces:
`paperless-drop install --thunar`, `--dolphin`, `--nautilus`, `--cosmic`, `--openwith`, `--watcher`.
A flag installs only that
piece (for example `--thunar` does not touch the service or the inbox folder).

### 6. Try it

1. Right-click a PDF → **Send to Paperless**. A notification should appear. Click it to open the
   document.
2. Copy a PDF into `~/Paperless-Inbox`. It should be uploaded within a few seconds and moved to
   `sent/<today>/`.
3. Send the same file again and you should get "Already in Paperless".

**Tip:** point your browser's download folder (or "ask where to save") at `~/Paperless-Inbox` and the
manual download step disappears.

---

## The inbox folder

```
~/Paperless-Inbox/
  sent/YYYY-MM-DD/   uploaded or already-present files; folders older than retention_days are deleted
  failed/            files Paperless rejected, each with <name>.error.txt saying why
  .processing/       files being uploaded right now
```

- If Paperless is unreachable (Tailscale down, server restarting) or the token is wrong, files
  **stay in the inbox** and are retried every minute or more, backing off to 10 minutes. You get one
  notification per outage, not one per file.
- To retry a file from `failed/`, move it back into the inbox.
- Watch the log: `journalctl --user -u paperless-drop -f`
- Each computer has its own inbox. Duplicates across computers are caught by checksum.

## Configuration

`~/.config/paperless-drop/config.toml` (created by `init`):

```toml
url = "https://docs.example.com"
tags = []              # extra tag names for every upload (the tags must already exist in Paperless)
wait = true            # wait for Paperless to finish, then report the document
notify = true

[inbox]
path = "~/Paperless-Inbox"
retention_days = 30    # keep local copies in sent/ this long; 0 = forever
```

Environment overrides: `PAPERLESS_DROP_URL`, `PAPERLESS_DROP_TOKEN`. After editing the config,
restart the watcher: `systemctl --user restart paperless-drop`.

## Commands

```
paperless-drop send FILE...      upload files (--force, --title, --tag NAME, --no-wait, --no-notify, -q)
paperless-drop watch [--once]    run the inbox watcher (the service does this)
paperless-drop doctor            check config, token, server, tags and installed integrations
paperless-drop init              create the config and save the token (--url, --keyring)
paperless-drop install           install the integrations (--thunar --dolphin --nautilus --cosmic --openwith --watcher)
paperless-drop uninstall         remove everything `install` added
```

## Updating

Get the new code onto the machine as in step 2, then:

```fish
uv tool install --reinstall <wheel-or-folder>
systemctl --user restart paperless-drop      # the watcher keeps running the old code until restarted
```

No need to re-run `init` or `install` unless the release notes say so.

## Troubleshooting

| Problem | Fix |
|---|---|
| `paperless-drop: command not found` | `~/.local/bin` is not on your `PATH` (step 1) |
| `doctor`: cannot reach the server | Tailscale is down, or the URL is wrong. Open the URL in a browser |
| `doctor`: token rejected (401) | Wrong or regenerated token. Copy the current one from My Profile and re-run `init` |
| No right-click entry in your file manager | Restart it (table in step 5). Check that `paperless-drop doctor` shows its entry as installed |
| Nautilus: can't find the entry | It is under **right-click → Scripts → Send to Paperless**, and only on a selected file |
| COSMIC Files: `install --cosmic` refuses | Your `context_actions` file has a shape it can't safely edit (for example a comment after the last entry). It prints the snippet to add by hand |
| Uploaded, but no notification | Is a notification daemon running? Try `notify-send test` |
| Clicking the notification does nothing | Is `xdg-open` installed? (`doctor` checks.) Click within about 20 seconds |
| Files sit in the inbox and never upload | `systemctl --user status paperless-drop` and `journalctl --user -u paperless-drop` |
| A file lands in `failed/` | Read its `.error.txt`. "Paperless could not read the file" means a damaged or fake PDF |
| Watcher does not start at login | Needs a systemd user session. Otherwise start `paperless-drop watch` from your desktop's autostart |

## Uninstall

```fish
paperless-drop uninstall          # removes the menu entries, the service and the integrations it added; nothing else
uv tool uninstall paperless-drop
```
Your config, token and `~/Paperless-Inbox` are left in place. Delete them by hand if you want.

## Development

```fish
env PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The tests run against an in-process fake Paperless server (`tests/fake_paperless.py`) whose responses
match a real Paperless-ngx 3.1.3. Change them together if Paperless's API changes.
