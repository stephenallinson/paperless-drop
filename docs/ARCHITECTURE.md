# paperless-drop — Architecture

Status: **Part 1 implemented and tested; Part 2 is a runbook for you to apply** (2026-10-09).
Sections below describe the design as built. Where the build differs from the first draft,
the text has been updated. The *Changes during implementation* section at the end lists them.

## Goal

Get a document from the desktop into Paperless-ngx with titles, tags, correspondent,
type and date filled in **without opening the web UI**.

Today the workflow is: download → open docs.stephenallinson.com → drag and drop → wait →
open the document → ask the built-in AI for suggestions → click to apply each one.

Target: save or right-click the file → a desktop notification appears → the document is
already in Paperless with its metadata set.

The project has two independent parts:

| Part | Runs on | What it does |
|---|---|---|
| **1. paperless-drop** (client) | Linux desktop (Thunar, Dolphin, Nautilus, COSMIC Files) | Gets files into Paperless: CLI, right-click menu, watched inbox folder |
| **2. paperless-gpt** (server) | svrubuntu01, next to Paperless | Runs automatic OCR and metadata on every new document using OpenRouter |

They're loosely coupled. The only contract between them is a **Paperless tag** that
triggers paperless-gpt. Either part is useful without the other.

```
 Desktop                                    svrubuntu01 (via Tailscale)
 ┌──────────────────────────────┐          ┌──────────────────────────────────────────┐
 │ Dolphin / Thunar right-click ─┐│          │ Paperless-ngx                             │
 │ "Open with → Send to Paperless"┼┼─ HTTPS ─▶  POST /api/documents/post_document/      │
 │ ~/Paperless-Inbox (watcher)  ─┘│  API     │      │ consume (Tesseract OCR)           │
 │          │                     │  token   │      ▼                                   │
 │   paperless-drop send          │          │  Workflow: "on added → add tag `ai-inbox`"│
 │          │                     │          │      │                                   │
 │   notify-send ◀── poll task ───┼──────────┤      ▼                                   │
 └──────────────────────────────┘          │ paperless-gpt ── OpenRouter (gpt-6-luna)   │
                                           │   └─ LLM OCR where needed, then title,     │
                                           │      tags, correspondent, type, date       │
                                           └──────────────────────────────────────────┘
```

---

## Part 1 — paperless-drop (desktop client)

### 1.1 Principles

- **Python standard library only** (Python ≥ 3.11 for `tomllib`; 3.14 is installed). No venv to
  break when Arch updates Python. External tools used: `notify-send`, `secret-tool`, `xdg-open`,
  all already present.
- **One core, many entry points.** Every integration (right-click, Open-With, watcher) calls
  the same `paperless-drop send` code path.
- **Never lose a file.** Files are only moved or deleted after Paperless confirms the result.
  Failures stay visible on disk with an error note.

### 1.2 Commands

```
paperless-drop send FILE...        Upload one or more files, wait for consumption, notify
paperless-drop watch               Long-running inbox watcher (run by systemd)
paperless-drop watch --once        Process the inbox once and exit (debugging)
paperless-drop doctor              Check config, token, connectivity, tag IDs, server version
paperless-drop install [--dolphin] [--thunar] [--openwith] [--watcher]
paperless-drop uninstall           Remove all integrations it installed
```

`send` options: `--tag NAME` (repeatable), `--title`, `--no-wait`, `--no-notify`, `--quiet`.

### 1.3 Upload flow (`send`)

1. **Validate.** The file exists and its extension is one Paperless can consume (pdf, png, jpg,
   jpeg, tiff, webp, gif, txt; office/eml only if Tika is enabled on the server). Other files
   are rejected immediately with a notification.
2. **Resolve tags.** Look up configured tag names to IDs with `GET /api/tags/?name__iexact=…`
   (once per invocation or watcher pass, only when tags are configured). A missing tag is an
   error that blocks the upload. The client never creates tags implicitly.
3. **Upload.** Send `POST /api/documents/post_document/` as multipart, with fields `document`,
   optional `title`, and repeated `tags`. The response is a task UUID. Multipart encoding is
   built by hand on `urllib.request` (about 30 lines).
4. **Wait** (unless `--no-wait`). Poll `GET /api/tasks/?task_id=<uuid>` with backoff (1 s → 5 s,
   5 min timeout). Possible outcomes:
   - `SUCCESS`: take `related_document` as the new document ID.
   - `FAILURE` whose message contains "duplicate": a **Duplicate** outcome, treated as done.
   - Other `FAILURE`: an error with the server message.
   - Timeout, or the server becomes unreachable while polling: **Queued**. The upload already
     succeeded, so the file is never re-uploaded.
5. **Notify.**
   - Success: "Added to Paperless: <title> (#123)", with an **Open** action that runs
     `xdg-open https://docs.stephenallinson.com/documents/123/details`.
   - Errors use critical urgency. Several files sent together produce one summary notification.
6. **Exit code.** 0 if all files succeeded or were duplicates, non-zero otherwise, so scripts
   can rely on it.

Uploads are sequential. Personal volumes don't need parallelism, and sequential uploads keep
the logs readable.

### 1.4 Watched inbox

**Folder layout:**

```
~/Paperless-Inbox/            ← drop or save files here (browser "Save as" target too)
  .processing/                ← claimed files currently uploading (hidden)
  failed/                     ← upload failed: file + file.error.txt
  sent/YYYY-MM-DD/            ← uploaded or duplicate; folders older than `retention_days` are deleted
```

**Mechanism.** A **long-running `paperless-drop watch` process** runs as a systemd user
service. It polls the top-level inbox with `os.scandir` every 3 s, which costs effectively
nothing.

The alternatives I considered and rejected:

- `.path` units (`DirectoryNotEmpty=` / `PathChanged=`). If a file has to stay in the
  inbox (a partial download, for example), the path unit retriggers in a tight loop and trips
  systemd's trigger rate limit, which leaves the unit failed. Handling that correctly needs
  more machinery than polling.
- inotify via `ctypes`. It works, but it's extra code for no user-visible benefit, and it
  misses events on some network or sync filesystems.

**Processing rules per file:**

1. **Ignore** dotfiles, subdirectories, and in-progress download suffixes (`.part`,
   `.crdownload`, `.download`, `.tmp`, `.partial`).
2. **Settle check.** The size and mtime must be unchanged across two scans, and the mtime
   must be at least 3 s old. This protects against slow copies and sync tools.
3. **Claim.** Use `os.rename` into `.processing/`. The rename is atomic, so the watcher and a
   concurrent right-click on the same file can't both upload it.
4. Run the `send` flow.
5. **On Success, Duplicate or Queued**, move the file to `sent/YYYY-MM-DD/`. **On failure**, move it to
   `failed/` and write `<name>.error.txt` holding the server message and timestamp. Failed
   files are **not** retried automatically. To retry, move the file back into the inbox.
6. **On startup**, anything left in `.processing/` (from a crash) goes back into the inbox.
7. **Once a day**, delete `sent/YYYY-MM-DD/` folders older than `retention_days`. The default
   is 30; `0` keeps them forever. Folders that don't match the date pattern are never touched.
8. **Service problems are not file problems.** If Paperless is unreachable, returns a 5xx,
   rejects the token, or a configured tag is missing, the file goes back to the inbox untouched.
   You get one notification per outage, and the watcher retries with back-off (60 s up to 10 min).

**systemd user unit:** `~/.config/systemd/user/paperless-drop.service`, with
`Restart=on-failure` and `WantedBy=default.target`. Logs go to journald, viewable with
`journalctl --user -u paperless-drop`.

**Browser tip** (documentation only, no code): set the browser to ask where to save, or add
a download rule, and save straight into `~/Paperless-Inbox`. That removes the separate
download step entirely.

### 1.5 Right-click and "Open With"

All integrations are installed by `paperless-drop install` and removed by `uninstall`. Each one only
runs `paperless-drop send` on the selected files. A default install adds those for file managers it
finds on the machine; flags install individual ones.

| Integration | File | Notes |
|---|---|---|
| **Dolphin** service menu | `~/.local/share/kio/servicemenus/paperless-drop.desktop` | Restricted to supported MIME types. Plasma 6 requires the file to be **executable**. Appears as "Send to Paperless". |
| **Thunar** custom action | `~/.config/Thunar/uca.xml` | Merged into the existing file, idempotently, keyed on a fixed `<unique-id>`. Your other actions are preserved, and a one-time backup is written next to it (`uca.xml.paperless-drop.bak`). Restart Thunar (`thunar -q`) to load it. |
| **Nautilus** script | `~/.local/share/nautilus/scripts/Send to Paperless` | Nautilus has no top-level custom actions, so it appears under **Scripts**. A small executable `sh` script that passes the selected files as arguments and exits quietly when none are selected (right-click on the background). Carries a marker line, so we never overwrite or delete a script we didn't create. |
| **COSMIC Files** action | `~/.config/cosmic/com.system76.CosmicFiles/v1/context_actions` | A single RON list of actions, undocumented officially (COSMIC ≥ 1.0.10); format taken from `src/context_action.rs`. Our entry lives between `// paperless-drop: begin/end` comments, so installs are idempotent and a user's own actions survive install and uninstall. `selection: Files` limits it to file selections. The backup goes *outside* `v1/` because cosmic-config reads every file in that directory as a setting. The file is read only at startup. If its shape is ambiguous (no list, or a trailing comment), we refuse and print the snippet. |
| **Open With** desktop entry | `~/.local/share/applications/paperless-drop.desktop` | Deliberately declares **no MIME types**. A declared type makes the app a candidate *default* handler, and the first build hijacked double-click for PDFs, images and text files. It is selectable via Open With → Other Application…, and appears in the application launcher. |

### 1.6 Configuration

`~/.config/paperless-drop/config.toml`:

```toml
url = "https://docs.stephenallinson.com"

# Token lookup order: $PAPERLESS_DROP_TOKEN → token file → Secret Service (secret-tool)
# Default token file: ~/.config/paperless-drop/token (mode 600 enforced). `paperless-drop init`
# writes it (or uses the keyring with --keyring).

tags = []                 # extra tag names added on upload (normally empty, see 2.4)
wait = true               # wait for consumption and report the document ID
notify = true

[inbox]
path = "~/Paperless-Inbox"
poll_seconds = 3
settle_seconds = 3
retention_days = 30
```

The client's token belongs to your own Paperless user, so uploaded documents are owned by you.

### 1.7 Code layout

```
paperless-drop/
  pyproject.toml                 # entry point paperless-drop = paperless_drop.cli:main
  src/paperless_drop/
    cli.py                       # argparse subcommands: send, watch, doctor, init, install, uninstall
    config.py                    # TOML load, defaults, token resolution
    api.py                       # PaperlessClient: tags, upload (multipart), task polling
    sender.py                    # send_file(): validation, upload, wait, Outcome
    inbox.py                     # scan / settle / claim / archive / prune
    watcher.py                   # polling loop, outage back-off
    notify.py                    # notify-send wrapper, Open action
    install.py                   # Dolphin, Thunar, Nautilus, COSMIC Files, Open-With, systemd unit
  tests/                         # unittest + an in-process fake Paperless (http.server)
  docs/ARCHITECTURE.md, docs/SERVER-RUNBOOK.md
  README.md
```

Installation: `uv tool install .` (or `pipx install .`), which puts `paperless-drop` on PATH
in `~/.local/bin`. Then run `paperless-drop install`.

### 1.8 Testing

- **Unit tests** with `unittest` against a fake Paperless server (`http.server` in a
  thread). It implements `post_document`, `tasks` (with success, duplicate, failure and slow
  cases) and `tags`.
- **Inbox tests** in a temp directory: a partial download is ignored, a growing file waits,
  concurrent claims upload only once, crash recovery from `.processing/` works, and
  retention pruning works.
- **Manual end-to-end check** against the real server with a throwaway PDF, then deleted.
  *Not yet done: it needs your API token.* See *Remaining verification* below.

---

## Part 2 — paperless-gpt (server side, svrubuntu01)

Applied by you from [SERVER-RUNBOOK.md](SERVER-RUNBOOK.md). Summary of the design:

### 2.1 What it does

paperless-gpt (icereed/paperless-gpt, v0.29.0, released 2026-10-05) is a sidecar container. It
polls Paperless for documents carrying a trigger tag, sends them to an LLM and **writes the
results back itself**: title, tags, correspondent, document type and created date, plus LLM
vision OCR text. Nothing waits for a click.

### 2.2 Pipeline

```
paperless-drop upload
   └▶ Paperless consumes the file (Tesseract)
        └▶ Paperless workflow "Document Added" ─ assigns tag `ai-inbox`
             └▶ paperless-gpt workflow "Inbox" (trigger tag `ai-inbox`, OCR enabled)
                  1. OCR the pages that need it (digital pages with a clean text layer are skipped)
                  2. generate and apply title / tags / correspondent / type / date
                  3. remove `ai-inbox`, add `paperless-gpt-auto-complete`
```

Both steps run inside a single paperless-gpt workflow, so paperless-gpt guarantees the order.
Because the trigger tag is assigned by a **Paperless workflow**, it applies to every ingestion
path (paperless-drop, web UI, phone, email), and paperless-drop stays generic (`tags = []`).

### 2.3 Models

| Role | Model | Price (OpenRouter, per million tokens, in / out) |
|---|---|---|
| Metadata | `openai/gpt-6-luna` | $0.10 / $0.50 |
| OCR (vision) | `openai/gpt-6-luna` | $0.10 / $0.50 |

About $0.0005 per scanned page. Born-digital PDFs skip the OCR pass. Use pinned slugs, not the
floating `~…-latest` aliases or the asynchronous `:batch` variants. If OCR quality disappoints on
real scans, `z-ai/glm-5.3-flash` (also vision-capable) is the alternative to compare, by changing
`VISION_LLM_MODEL`.

### 2.4 Behaviour that matters

- **Content, not files.** With LLM OCR, paperless-gpt replaces the document's *content* text. It
  never modifies the original file. (Searchable-PDF output exists only with Google Document AI,
  so `PDF_UPLOAD*` is not used.)
- **Retries and failure.** After `AUTO_TAG_MAX_RETRIES` failures the trigger tag is removed and
  `paperless-gpt-failed` added, so a bad document stops being re-billed.
- **Tags.** Your library started with 3 tags, so `CREATE_NEW_TAGS` begins as `true` and moves to
  `false` once you have curated the tag set (runbook step 3).

### 2.5 Security and cost

- The paperless-gpt UI has no login of its own. It is bound to `127.0.0.1` and reached by SSH
  tunnel. Paperless itself is tailnet-only.
- It gets its own Paperless user and token, and a dedicated OpenRouter key with a credit limit.
- Secrets live in the compose `.env`, not in YAML.
- OCR sends full page images to OpenRouter and the model provider. The runbook asks you to
  review OpenRouter's privacy settings first.

### 2.6 Rollout

Auto-apply from day one. A one-time smoke test (3 documents, step 9 of the runbook) runs with the
Paperless workflow disabled; then it is enabled and nothing waits on a person. Two optional saved
views ("AI processed (7 days)" and "AI needs attention") exist for spot checks and are never a
queue.

---

## Implementation status

1. **Part 1a** (`api`, `config`, `sender`, `send`, `doctor`, `init`): done and tested.
2. **Part 1b** (`install` for Open With, Dolphin, Thunar): done; file formats validated, installed
   only into a throwaway HOME so far. Dolphin is not installed on this machine, so the Dolphin
   menu is untested in a real Dolphin.
3. **Part 1c** (`watch` inbox and systemd unit): done and tested; the unit passes
   `systemd-analyze --user verify`.
4. **Part 2**: `docs/SERVER-RUNBOOK.md` written, for you to apply.

61 unit tests pass (`env PYTHONPATH=src python3 -m unittest discover -s tests -t .`).

## Remaining verification (needs your token or your desktop)

- Run `paperless-drop init` and `doctor`, then send a real PDF. Two assumptions come from the
  Paperless 2.x docs and need confirming against 3.1.3: the shape of the `/api/tasks/` response
  (`status`, `result`, `related_document`) and the `duplicate` failure message. If either differs,
  the failure mode is mild: the file is reported as *Queued* instead of *Added*.
- `doctor` may show the Paperless version as unknown. The server did not send an `X-Version`
  header on the unauthenticated 401 I probed, and it may do so on authenticated responses.
- Click-test the notification's **Open** button and the Thunar menu on your desktop.

## Changes during implementation

| Draft | As built | Why |
|---|---|---|
| `sent/YYYY-MM/`, retention by file | `sent/YYYY-MM-DD/`, retention by folder | File mtimes are the download time, not the archive time, so per-file ages would be wrong. Dated folders need no timestamps. |
| Tag-ID cache on disk | Resolved per run | A cache that can go stale costs more than one small request. |
| Thunar: "rewrites uca.xml on exit, so run `thunar -q` first" | Merge in place with a backup; you restart Thunar | My claim about Thunar was unverified, and quitting your open window unasked would be rude. |
| Open With entry hidden (`NoDisplay`) | Visible | Hidden entries don't show in Open With menus. |
| Part 2: two Paperless workflows chaining OCR then metadata | One Paperless workflow + one paperless-gpt workflow with OCR enabled | paperless-gpt's own workflows run OCR before metadata, which removes two unverified assumptions. |
| `PDF_UPLOAD_MODE=version` and `OCR_SKIP_DIGITAL_PAGES` together | Only the latter | Searchable-PDF output is Google Document AI only, and it switches skipping off. |
| Metadata model GLM 5.3 Flash | `openai/gpt-6-luna` | Your decision. |
| Service errors | Distinct from file errors | Files are never parked in `failed/` because Paperless was down or the token was wrong. |

## Decisions (review 2026-10-09)

| # | Question | Decision |
|---|---|---|
| 1 | Paperless-ngx version | **3.1.3** |
| 2 | Exposure | **Tailnet-only**. paperless-gpt's UI stays off the proxy and is reached by SSH tunnel. |
| 3 | Server changes | **A runbook that you apply.** I don't touch svrubuntu01. |
| 4 | Models | **`openai/gpt-6-luna`** via OpenRouter for both OCR and metadata. GLM 5.3 Flash is the alternative to compare. |
| 5 | Auto-apply | **Yes, from day one.** Saved views are for spot checks only. |
| 6 | Inbox, retention, token | `~/Paperless-Inbox`, **30 days for local copies in `sent/`** (Paperless keeps documents forever), and your own Paperless user's token for the client |
