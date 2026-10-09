# Server runbook: automatic metadata (and on-demand OCR) with paperless-gpt

Apply these steps on **svrubuntu01**. Nothing here touches your desktop, and
paperless-drop doesn't need any change: it uploads, and the server does the rest.

Pipeline (no human step for normal documents):

```
paperless-drop upload ─▶ Paperless consumes the file (its own OCR text)
   ─▶ Paperless workflow adds tag `paperless-gpt-auto`
   ─▶ paperless-gpt: title, tags, correspondent, document type, created date applied
   ─▶ `paperless-gpt-auto` removed, `paperless-gpt-auto-complete` added

Poor scan?  Add tag `paperless-gpt-ocr-auto` to it ─▶ LLM vision OCR replaces its text
   ─▶ `paperless-gpt-ocr-auto` removed, `paperless-gpt-ocr-complete` added
```

Models: `openai/gpt-6-luna` through OpenRouter, for both metadata and OCR
($0.10 / $0.50 per million tokens). Luna is a *reasoning* model: its hidden "thinking" tokens are
billed as output, so **measure the real cost in the smoke test (step 9)** before turning the
automation on.

## What version this targets, and what it can't do

This targets paperless-gpt **v0.29.0** (released 2026-10-05), checked against that release's source
code. Two features that look attractive in the project's current docs are **not in v0.29.0**; both
landed on `main` a day or more after the release:

| Feature | Landed | Effect here |
|---|---|---|
| **AI Workflows** (per-trigger-tag pipelines, OCR then metadata in one pass) | 2026-10-06 | Not available. We use the two plain tags instead. |
| **`OCR_SKIP_DIGITAL_PAGES`** (don't LLM-OCR pages that already have clean text) | 2026-10-08 | Not available. So we do **not** LLM-OCR every document: OCR is on demand only. |

That is why automatic OCR of everything is deliberately left out: without the skip feature, every
born-digital PDF would be re-OCR'd by the LLM for no benefit. When a release containing both ships,
see *Upgrading later* at the end.

---

## 0. Before you start

Your setup (confirmed from the server):

- Stack folder: `~/dockercompose/paperless`, compose file `docker-compose.yaml`, secrets in `.env`
  (gitignored). Services: `webserver`, `db`, `broker`. Run every `docker compose` command below
  from that folder.
- Backups: your restic job already covers this folder. Confirm it ran recently before you start,
  or take a one-off export: `docker compose exec webserver document_exporter ../export`.
- Dockge manages your stacks. Editing the file by hand is fine; Dockge shows the stack as changed.
- The Paperless stack's `backend` network is `internal: true` (**no internet**), so paperless-gpt
  also joins a second, normal network to reach OpenRouter (step 5).

## 1. OpenRouter key

1. At <https://openrouter.ai/settings/keys> create a **new key** named `paperless-gpt`.
2. Set a **credit limit** on that key. $5 is plenty to start. If something loops, the limit
   caps the damage.
3. Privacy: metadata generation sends document *text* to OpenRouter and the model provider, and
   on-demand OCR sends page *images* (statements, IDs, letters). Review your OpenRouter privacy
   settings (prompt logging, training use, and whether to restrict routing to providers with zero
   data retention) before you enable this.

## 2. A Paperless user and token for paperless-gpt

**Use your own token.** In Paperless, open *My Profile → API Auth Token* for your normal user
(`stephen`) and copy the existing token. It is the same one your desktop's paperless-drop uses; do
**not** press regenerate, which would break every other computer.

Why not a dedicated service user? Paperless gives every tag, correspondent and document type to the
user whose token **created** it, and hides it from everyone else. If paperless-gpt used its own user,
every tag, correspondent and type it created would show up in your UI as an italic **Private**, and
you couldn't see or edit them. (This happened in the first test run. Documents themselves stay
owned by you either way.) With your own token, everything paperless-gpt creates is yours. The cost
is attribution: its edits show in document history as you.

If you ever do use a separate user, then **after** it has created objects, log in as an admin and
use the bulk *Permissions* action on Tags, Correspondents and Document types to set the owner or
share them with your user.

## 3. Tags in Paperless

Your library currently has only three tags (`benefits`, `homelab`, `invoice`), so decide how paperless-gpt
should treat tags before it runs (this is the `CREATE_NEW_TAGS` setting in step 5):

- **Recommended: `"true"` for the first couple of weeks.** With three tags to choose from, the model
  can barely tag anything. Let it propose tags, then review the Tags page after a week or two, merge
  or delete the odd ones, and set `CREATE_NEW_TAGS` to `"false"` once the set looks right.
- **Or: `"false"` from the start**, after you create a starter set of 10–15 tags you want
  (for example `tax`, `insurance`, `medical`, `receipt`, `contract`, `banking`, `home`, `vehicle`).
  Tighter, but you do the up-front work.

Now create these tags (*Tags → Create*). For all of them, set **Matching algorithm = None** and
leave **Inbox tag = off**. The default matching algorithm is "Auto", which would let Paperless's own
classifier start assigning these control tags by itself:

| Tag | Purpose |
|---|---|
| `paperless-gpt-auto` | **Trigger for automatic metadata.** A Paperless workflow adds it to every new document (step 7). |
| `paperless-gpt-ocr-auto` | **Trigger for LLM OCR.** You add it by hand to a poor scan. |
| `paperless-gpt` | Manual review: the document appears in paperless-gpt's UI to preview suggestions before applying. |

paperless-gpt creates `paperless-gpt-auto-complete`, `paperless-gpt-failed` and
`paperless-gpt-ocr-complete` itself, and they get the default "Auto" matching. **When they appear
(after the first run, step 8 or 9), open each and set its Matching algorithm to None too.**

(If you already created an `ai-inbox` tag for the earlier draft of this plan, leave it. It is
unused until *Upgrading later*.)

## 4. Secrets in the compose `.env`

Append them without typing them into your shell history (the prompts hide your input):

```bash
cd ~/dockercompose/paperless
[ -n "$(tail -c1 .env)" ] && echo >> .env          # make sure .env ends with a newline
read -rsp 'OpenRouter key: ' K; echo; printf 'OPENROUTER_API_KEY=%s\n' "$K" >> .env; unset K
read -rsp 'paperless-gpt Paperless token: ' T; echo; printf 'PAPERLESS_GPT_TOKEN=%s\n' "$T" >> .env; unset T
chmod 600 .env
cut -d= -f1 .env      # names only: expect POSTGRES_PASSWORD, PAPERLESS_SECRET_KEY, PAPERLESS_UID, PAPERLESS_GID, + the 2 new ones
```

## 5. Add the container

Three edits, all in `~/dockercompose/paperless`.

**a) `docker-compose.yaml`: add an internet-facing network.** Your existing `networks:` block at the
bottom becomes:

```yaml
networks:
  backend:
    internal: true
  npm:
    external: true
    name: npm_default
  egress: {}          # NEW: a normal network so paperless-gpt can reach OpenRouter
```

**b) `docker-compose.yaml`: add the service** under `services:` (after `webserver`). The image is
pinned by digest like your other images (v0.29.0, released 2026-10-05):

```yaml
  paperless-gpt:
    image: icereed/paperless-gpt:v0.29.0@sha256:4127ea223e0f496c1075a3a05661df4847140974c3c3ce89a4ba991be50320cb
    restart: unless-stopped
    depends_on:
      - webserver
    environment:
      PUID: ${PAPERLESS_UID}               # same user as Paperless, so files in ./paperless-gpt are yours
      PGID: ${PAPERLESS_GID}
      PAPERLESS_BASE_URL: http://webserver:8000   # reached over the internal `backend` network
      PAPERLESS_API_TOKEN: ${PAPERLESS_GPT_TOKEN}

      # Metadata (text) LLM, via OpenRouter's OpenAI-compatible API
      LLM_PROVIDER: openai
      LLM_MODEL: openai/gpt-6-luna
      OPENAI_BASE_URL: https://openrouter.ai/api/v1
      OPENAI_API_KEY: ${OPENROUTER_API_KEY}
      LLM_LANGUAGE: English
      TOKEN_LIMIT: 8000

      # On-demand OCR via a vision LLM (only runs on documents you tag paperless-gpt-ocr-auto)
      OCR_PROVIDER: llm
      VISION_LLM_PROVIDER: openai
      VISION_LLM_MODEL: openai/gpt-6-luna
      OCR_PROCESS_MODE: image
      OCR_LIMIT_PAGES: 10                  # WARNING: pages past the limit are dropped from the document's text; 0 = no limit
      OCR_MAX_RETRIES: 3

      # Safety
      CREATE_NEW_TAGS: "true"              # see step 3: switch to "false" after a couple of weeks
      AUTO_TAG_MAX_RETRIES: 3
      LOG_LEVEL: info
    volumes:
      - ./paperless-gpt/prompts:/app/prompts
      - ./paperless-gpt/config:/app/config
      - ./paperless-gpt/db:/app/db
    ports:
      - "127.0.0.1:8080:8080"              # localhost only; reach it by SSH tunnel (ports 8000/8080/8088 are free)
    networks:
      - backend                            # to reach Paperless
      - egress                             # to reach OpenRouter
    logging: *logging
```

If you already added the service from the earlier draft, **delete the line
`OCR_SKIP_DIGITAL_PAGES: "true"`**. v0.29.0 silently ignores it, so it only gives false comfort.
Then apply with `docker compose up -d paperless-gpt` (this recreates only that container).

**c) `.gitignore`: add** (prompts stay tracked; runtime state does not):

```
/paperless-gpt/config/
/paperless-gpt/db/
```

Then check the file is valid before starting anything (no output means it is valid):

```bash
docker compose config -q && echo "compose file OK"
```

Notes on what I deliberately left out:

- No `PDF_UPLOAD`, `PDF_UPLOAD_MODE` or `CREATE_LOCAL_PDF`. Searchable-PDF and hOCR output only
  work with Google Document AI. With LLM OCR, paperless-gpt writes the recognised text into the
  document's **content** field and **never modifies the original file**.
- `TOKEN_LIMIT: 8000` only limits how much text goes to the metadata prompt, which is plenty
  for titles and tags. Raise it if long documents get poor results.
- paperless-gpt is **not** on the `npm` network and has no proxy entry. Its UI has no login of its own.

## 6. (Reserved)

The earlier draft created a paperless-gpt workflow file here. That feature isn't in v0.29.0, so
there is nothing to do in this step. If you created
`paperless-gpt/prompts/workflows/inbox/workflow.json`, it is ignored and harmless. Leave it for
*Upgrading later*, or delete the `workflows` folder.

## 7. The Paperless workflow that adds the trigger tag

In Paperless: *Workflows → Add*.

- **Name:** `AI: generate metadata for new documents`
- **Trigger:** *Document Added*. No filters, so it applies to every ingestion path (paperless-drop,
  web UI, phone app, email).
- **Action:** *Assignment* → *Assign tags* = `paperless-gpt-auto`.

Labels may differ slightly between Paperless-ngx 3.x versions. What matters is the trigger
type, "Document Added", and an action that assigns the tag.

⚠️ **Pick the right one of two near-identical tags.** `paperless-gpt-auto` (with `-auto`) is
processed automatically. `paperless-gpt` (without it) is the *manual review* tag: the document just
sits in the paperless-gpt UI until you click *Generate Suggestions*. Assigning the wrong one gives
exactly the symptom "I still have to click Generate Suggestions". To check what actually happened,
look at a new document's tags: it should end up with `paperless-gpt-auto-complete`, never stuck on
`paperless-gpt`.

⚠️ Leave this workflow **disabled** until the smoke test (step 9) passes.

## 8. Start it

First, one check that Paperless will accept requests addressed to `webserver` (it will be called by
that name, not by `docs.stephenallinson.com`). Expect `200`, `301`, `302` or `401`, **not** `400`:

```bash
docker compose exec webserver curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: webserver:8000' http://localhost:8000/api/
```

(If that prints `400`, add `PAPERLESS_ALLOWED_HOSTS: "webserver,docs.stephenallinson.com"` to the
`webserver` environment and run `docker compose up -d webserver`.)

Optionally confirm the networks and port binding (`docker compose config` sorts keys alphabetically,
so filter the whole service block rather than counting lines after its name):

```bash
docker compose config | awk '/^  paperless-gpt:/{f=1;next} /^  [a-z-]+:$/{f=0} f' | grep -E '^    networks:|^      (backend|egress):|host_ip|published|target'
```

Expect `backend`, `egress`, `host_ip: 127.0.0.1`, `published: "8080"` and `target: 8080`.

Then start only the new service. This does not touch your other containers:

```bash
docker compose up -d paperless-gpt
docker compose logs paperless-gpt 2>&1 | grep -v '\[GIN\]' | head -50
```

(The `[GIN]` lines are the web UI polling every 5 seconds while a browser tab is open; filter them
out.) Expect `Using paperless-gpt-auto as auto tag`, `Using LLM OCR provider … model=openai/gpt-6-luna`,
and `OCR provider and processing mode configuration is valid`, with no errors.

To open its web UI (needed for the manual review flow and prompt tuning), from your desktop:

```fish
ssh -L 8080:localhost:8080 svrubuntu01      # then browse to http://localhost:8080
```

## 9. Smoke test (once, with the Paperless workflow from step 7 still disabled)

Use throwaway or already-processed documents. The earlier tests uploaded a few.

1. **Preview, no writes.** Add the tag `paperless-gpt` to one existing document. In the paperless-gpt
   UI (tunnel above) it appears in the list. Click **Generate Suggestions** and read the proposed
   title, tags, correspondent and type. Don't click Apply yet. This is the first real LLM call.
   - [ ] the suggestions are sensible, better than or equal to the current metadata
   - [ ] **cost:** open the OpenRouter activity page and note this request's cost and its token
         counts (input, output, and any "reasoning" tokens)
2. **Automatic metadata.** Send two test files with paperless-drop (a born-digital PDF such as a
   bill, and a scan or phone photo), then add the tag `paperless-gpt-auto` to each by hand.
   Watch `docker compose logs -f paperless-gpt | grep -v GIN`. For each document:
   - [ ] `paperless-gpt-auto` is gone and `paperless-gpt-auto-complete` is present
   - [ ] the title, correspondent and tags are sensible (new tags are expected while
         `CREATE_NEW_TAGS` is `"true"`)
   - [ ] the original file still downloads and looks right
3. **On-demand OCR.** On the scan, add the tag `paperless-gpt-ocr-auto`. When it finishes:
   - [ ] `paperless-gpt-ocr-auto` is gone and `paperless-gpt-ocr-complete` is present
   - [ ] open the document's *Content* tab: the text is better than Tesseract's (compare with the
         previous text if you can)
   - [ ] **cost per page:** from the OpenRouter activity page. If a page costs more than a cent or
         two, reasoning tokens are the likely cause: stop and tell me, and we'll look at a cheaper
         model or at limiting reasoning
4. **Turn it on.** Enable the Paperless workflow from step 7. Send one more file with paperless-drop
   and confirm the metadata arrives by itself with no clicks.

## 10. Two saved views, as a safety net

Not a queue. You never have to clear them.

- **AI processed (7 days):** *Tags: has `paperless-gpt-auto-complete`*, *Added: within the last
  week*. Enable "Show on dashboard". Use it for occasional spot checks.
- **AI needs attention:** *Tags: has `paperless-gpt-failed`*. Dashboard on. It should stay empty.

## 11. Tighten the tag prompt (recommended while `CREATE_NEW_TAGS` is `"true"`)

The default tag prompt lets the model invent tags that are "highly relevant". In the first test it
created `Insurance Quote Comparison`, a document title rather than a category. The prompt in
[`docs/prompts/tag_prompt.tmpl`](prompts/tag_prompt.tmpl) asks for 1–3 broad, reusable categories,
forbids titles, names and document kinds as tags, and prefers existing tags.

Apply it either way (paperless-gpt reloads prompts instantly, with no restart):

- In the paperless-gpt UI: **Settings** → the tag prompt → paste the file's contents → save; or
- On the server: replace `~/dockercompose/paperless/paperless-gpt/prompts/tag_prompt.tmpl` with it.

Keep the `{{...}}` placeholders exactly as they are. Then re-test with the manual flow (tag a
document `paperless-gpt`, **Generate Suggestions**, read, and apply or not).

## 12. Optional: automatic OCR for photos only

LLM OCR was excellent on a hard test (a floor plan with text at every angle), but do **not** tag every
new document `paperless-gpt-ocr-auto`:

- v0.29.0 cannot skip pages that already have clean text, so born-digital PDFs would be re-typed by the
  LLM: extra cost, and a chance of small errors in exact text such as invoice numbers.
- **OCR text replaces the document's content.** With `OCR_LIMIT_PAGES: 10`, pages 11 onward are
  silently left out of the searchable text (the original file is untouched). Set it to `0` if you
  deliberately OCR long documents on demand; the cost then scales with page count.

Photos and scans are where Tesseract is weakest and they are usually one page, so automate those.
*Consumption Started* triggers can filter by file name and by source (API, consume folder, mail);
*Document Added* cannot see the file name any more. Three Paperless workflows (each one a separate
entry under *Workflows*), replacing the single workflow from step 7:

| Workflow | Trigger and filters | Action: assign tag |
|---|---|---|
| **A. Metadata for everything except photos** | *Document Added*; **No Tags**: `paperless-gpt-ocr-auto` | `paperless-gpt-auto` |
| **B. OCR photos** | *Consumption Started*; filename `*.jpg` (add triggers for `*.jpeg`, `*.png`; a scanner's *consume folder* source works too) | `paperless-gpt-ocr-auto` |
| **C. Metadata after OCR** | *Document Updated*; **Any Tags**: `paperless-gpt-ocr-complete`; **No Tags**: `paperless-gpt-auto-complete`, `paperless-gpt-failed` | `paperless-gpt-auto` |

A photo then flows B (OCR) → C (metadata from the improved text); everything else flows A. B is
what A's filter checks for. ⚠️ Not yet tested on this server: try it with one photo, and confirm that
a single trigger accepts the filename pattern you use (otherwise add one trigger per extension).

## Day to day

- **Nothing to do** for normal documents.
- **A scan with bad text:** add `paperless-gpt-ocr-auto` to it. When `paperless-gpt-ocr-complete`
  appears, add `paperless-gpt-auto` again if you also want the title and tags regenerated from the
  better text.
- **Wrong metadata on one document:** just edit it in Paperless. paperless-gpt does not revisit
  documents once `paperless-gpt-auto` has been removed.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `401` / `403` in the logs | Wrong `PAPERLESS_GPT_TOKEN`, or the user lacks permission (step 2) |
| Tags or correspondents show as italic **Private** in your UI | They were created by a different Paperless user than the one you log in as (step 2). Switch `PAPERLESS_GPT_TOKEN` to your own token, then as an admin use bulk *Permissions* on Tags, Correspondents and Document types to set the owner to you, or share them: grant View and Edit to a group you belong to (for example *Document Managers*), which also works and keeps the original owner. |
| Logs show timeouts reaching `openrouter.ai` | The container isn't on the `egress` network, or the host has no outbound access. Check both networks are attached: `docker compose config \| awk '/^  paperless-gpt:/{f=1;next} /^  [a-z-]+:$/{f=0} f' \| grep -E '^      (backend\|egress):'` |
| `400` / `DisallowedHost` talking to Paperless | See the pre-flight check in step 8 (`PAPERLESS_ALLOWED_HOSTS`) |
| `404 model not found` | Check the slug at <https://openrouter.ai/models>. Keep `openai/gpt-6-luna` unless it has been retired. |
| `400` mentioning `temperature` | Some models reject temperature `0`. Add `VISION_LLM_TEMPERATURE: "1.0"` (the README documents this for GPT-5). |
| Documents get `paperless-gpt-failed` | See the log line just before it. After `AUTO_TAG_MAX_RETRIES` failures paperless-gpt gives up on that document (and stops billing for it). Fix the cause, remove `paperless-gpt-failed`, then re-add `paperless-gpt-auto`. |
| Documents stay on `paperless-gpt-auto` | The poll hasn't reached them yet (it works through 25 at a time), or the logs show an error for that document. |
| Metadata is mediocre | Tune prompts in the paperless-gpt UI under **Settings**; prompt files live in `paperless-gpt/prompts` (tracked in git). |
| Costs higher than expected | Check what is tagged `paperless-gpt-ocr-auto`, lower `OCR_LIMIT_PAGES`, and compare the per-request cost in the OpenRouter activity page. |

**Emergency stop:** disable the Paperless workflow (step 7), or run
`docker compose stop paperless-gpt`. Documents already processed keep their changes, and the
document history in Paperless shows what changed.

## Optional: commit the stack change

`~/dockercompose` is a git repository with many unrelated uncommitted changes, so add **only these
files** and never `git add -A`:

```bash
cd ~/dockercompose
git add paperless/docker-compose.yaml paperless/.gitignore paperless/paperless-gpt/prompts
git commit -m "paperless: add paperless-gpt (LLM metadata + on-demand OCR via OpenRouter)"
```

## Optional: backfill old documents

Tag existing documents with `paperless-gpt-auto` in batches of 20–50 (bulk edit in the Paperless
document list), then watch the cost for the first batch before doing more.

## Upgrading later (when workflows and skip-digital-pages ship in a release)

Watch <https://github.com/icereed/paperless-gpt/releases> (your Diun instance can alert on the image
tag). When a release newer than v0.29.0 includes **AI Workflows** and **`OCR_SKIP_DIGITAL_PAGES`**:

1. Read that release's notes and the `docs/workflows.md` **at that tag**, not on `main`.
2. Bump the image tag and digest in the compose file, add `OCR_SKIP_DIGITAL_PAGES: "true"`, and
   `docker compose up -d paperless-gpt`.
3. Create `paperless-gpt/prompts/workflows/inbox/workflow.json` with `"trigger_tag": "ai-inbox"`
   and `"enable_ocr": true` (OCR then metadata in one pass).
4. Change the Paperless workflow from step 7 to assign `ai-inbox` instead of `paperless-gpt-auto`.
5. Verify through `curl -s http://127.0.0.1:8080/api/workflows`, which should now list the workflow.
