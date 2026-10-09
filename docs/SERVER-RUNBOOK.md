# Server runbook: automatic OCR and metadata with paperless-gpt

Apply these steps on **svrubuntu01**. Nothing here touches your desktop, and
paperless-drop doesn't need any change: it uploads, and the server does the rest.

Target pipeline (no human step):

```
paperless-drop upload ─▶ Paperless consumes the file (Tesseract) ─▶ Paperless workflow adds tag `ai-inbox`
   ─▶ paperless-gpt workflow "Inbox": LLM OCR (skipping pages that already have text)
   ─▶ title, tags, correspondent, document type, date applied
   ─▶ `ai-inbox` removed, `paperless-gpt-auto-complete` added
```

Models: `openai/gpt-6-luna` through OpenRouter, for both OCR and metadata
($0.10 / $0.50 per million tokens). Luna is a *reasoning* model: its hidden "thinking" tokens are
billed as output. My estimate of about $0.0005 per scanned page ignores that, so **measure the real
cost in the smoke test (step 9) before enabling automation**.

Verified against paperless-gpt's README and `docs/workflows.md` on 2026-10-09 (release v0.29.0).
Items marked ⚠️ are things I could not confirm from the docs. The smoke test (step 9) checks them.

---

## 0. Before you start

- Make a backup you can restore, for example
  `docker compose exec webserver document_exporter ../export` (adjust the service name) or a
  snapshot of the Paperless data volumes.
- Know where your Paperless `docker-compose.yml` and `.env` live. Below, `<paperless-service>`
  is the name of the Paperless service in that compose file (`webserver` in the official one).
  The paperless-gpt container must be in the **same compose project** so it shares the network.

## 1. OpenRouter key

1. At <https://openrouter.ai/settings/keys> create a **new key** named `paperless-gpt`.
2. Set a **credit limit** on that key. $5 is plenty to start. If something loops, the limit
   caps the damage.
3. Privacy: OCR sends full page images (statements, IDs, letters) to OpenRouter and the model
   provider. Review your OpenRouter privacy settings (prompt logging, training use, and
   whether you want to restrict routing to providers with zero data retention) before you
   enable this. The built-in suggestions you already use send document *text*. OCR sends the
   page images.

## 2. A Paperless user and token for paperless-gpt

Documents uploaded by paperless-drop are **owned by your user**, so another user can only edit
them with explicit permission. Pick one:

- **A. Dedicated user (recommended).** Your normal Paperless user (`stephen`) is not a superuser, so
  log in with your **admin** account for this. Go to *Settings → Users & Groups → Add*, create
  `paperless-gpt`, and enable **Superuser status**. Its actions then show under that
  name in document history, and you can revoke it independently.
- **B. Your own token.** Fewer moving parts, but its edits appear as you.

Get the token:
- A: log in as `paperless-gpt` once, then *My Profile → API Auth Token → generate*.
  Or as admin, open `/admin/authtoken/tokenproxy/` and add a token for that user.
- B: *My Profile → API Auth Token* for your own user.

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
| `ai-inbox` | Trigger. A Paperless workflow adds it to every new document. |
| `paperless-gpt` | paperless-gpt's manual trigger (review in its UI) |
| `paperless-gpt-auto` | paperless-gpt's global auto trigger (not used by the pipeline, but handy for backfills) |

paperless-gpt creates `paperless-gpt-auto-complete`, `paperless-gpt-failed` and
`paperless-gpt-ocr-complete` itself at startup, and they get the default "Auto" matching. **After the
first start (step 8), open those three and set their Matching algorithm to None too.**

## 4. Secrets in the compose `.env`

Add to the `.env` next to your compose file (never into the YAML), then `chmod 600 .env`:

```
PAPERLESS_GPT_TOKEN=<token from step 2>
OPENROUTER_API_KEY=<key from step 1>
```

## 5. Add the container

Add this service to the **same** `docker-compose.yml`:

```yaml
  paperless-gpt:
    image: icereed/paperless-gpt:v0.29.0   # pinned; bump deliberately
    restart: unless-stopped
    depends_on:
      - <paperless-service>
    environment:
      PAPERLESS_BASE_URL: http://<paperless-service>:8000   # internal network
      PAPERLESS_API_TOKEN: ${PAPERLESS_GPT_TOKEN}

      # Metadata (text) LLM, via OpenRouter's OpenAI-compatible API
      LLM_PROVIDER: openai
      LLM_MODEL: openai/gpt-6-luna
      OPENAI_BASE_URL: https://openrouter.ai/api/v1
      OPENAI_API_KEY: ${OPENROUTER_API_KEY}
      LLM_LANGUAGE: English
      TOKEN_LIMIT: 8000

      # OCR via a vision LLM
      OCR_PROVIDER: llm
      VISION_LLM_PROVIDER: openai
      VISION_LLM_MODEL: openai/gpt-6-luna
      OCR_PROCESS_MODE: image
      OCR_LIMIT_PAGES: 10                  # 0 = no limit
      OCR_SKIP_DIGITAL_PAGES: "true"       # pages with clean text layers are not sent to the LLM
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
      - "127.0.0.1:8080:8080"              # not published; reach it by SSH tunnel
```

Notes on what I deliberately left out:

- No `PDF_UPLOAD`, `PDF_UPLOAD_MODE` or `CREATE_LOCAL_PDF`. Searchable-PDF and hOCR output only
  work with Google Document AI. With LLM OCR, paperless-gpt writes the recognised text into the
  document's **content** field and **never modifies the original file**. Those options also
  switch off `OCR_SKIP_DIGITAL_PAGES`.
- `TOKEN_LIMIT: 8000` only limits how much text goes to the metadata prompt, which is plenty
  for titles and tags. Raise it if long documents get poor results.

## 6. The paperless-gpt workflow (OCR, then metadata, in one pass)

Create the workflow as a file on the host, in the `prompts` volume:

```bash
mkdir -p paperless-gpt/prompts/workflows/inbox
cat > paperless-gpt/prompts/workflows/inbox/workflow.json <<'EOF'
{
  "version": 1,
  "name": "Inbox",
  "trigger_tag": "ai-inbox",
  "enable_ocr": true,
  "ocr_limit_pages": 10
}
EOF
```

Steps you leave out use the defaults, which are all `AUTO_GENERATE_*` = on: title, tags,
correspondent, document type and created date. With no `completion_tag`, finished documents get
`paperless-gpt-auto-complete`. paperless-gpt picks up the file within one poll cycle, with no
restart needed.

⚠️ If the `prompts` directory is created by Docker as root, make it writable by the container
user (`PUID`/`PGID` default to 10001): `sudo chown -R 10001:10001 paperless-gpt`.

## 7. The Paperless workflow that adds the trigger tag

In Paperless: *Workflows → Add*.

- **Name:** `AI: process new documents`
- **Trigger:** *Document Added*. No filters, so it applies to every ingestion path (paperless-drop,
  web UI, phone app, email).
- **Action:** *Assignment* → *Assign tags* = `ai-inbox`.

Labels may differ slightly between Paperless-ngx 3.x versions. What matters is the trigger
type, "Document Added", and an action that assigns the tag.

⚠️ Leave this workflow **disabled** until the smoke test (step 9) passes.

## 8. Start it

```bash
docker compose pull paperless-gpt
docker compose up -d paperless-gpt
docker compose logs -f paperless-gpt
```

You want to see it connect to Paperless without auth errors and register the `Inbox` workflow.
To open its web UI (rarely needed, for prompt tuning and testing):

```fish
ssh -L 8080:localhost:8080 svrubuntu01      # then browse to http://localhost:8080
```

## 9. Smoke test (once, with the Paperless workflow still disabled)

1. In paperless-gpt's UI, open **AI Workflows → Inbox → Test on a document**, pick an existing
   document, and check the proposed title and tags. Nothing is written.
2. On the desktop, send three test files with paperless-drop: a **born-digital PDF** (a bill),
   a **scan or phone photo**, and a **multi-page PDF**.
3. In Paperless, add the tag `ai-inbox` to each of the three by hand.
4. Wait a minute or two. Watch `docker compose logs -f paperless-gpt`. For each document, check:
   - [ ] `ai-inbox` is gone and `paperless-gpt-auto-complete` is present
   - [ ] the title, correspondent and tags are sensible (new tags are expected while
         `CREATE_NEW_TAGS` is `"true"`)
   - [ ] the content shows good text. Scans should be better than Tesseract's, and the
         born-digital PDF's text should be unchanged
   - [ ] the original file still downloads and looks right
   - [ ] **cost:** the OpenRouter activity page shows what each request cost. Note the cost per
         page for the scan and per document for the born-digital PDF. If a page costs more than a
         cent or two, reasoning tokens are the likely cause: stop, and we'll look at a cheaper
         model or at limiting reasoning before enabling the Paperless workflow
5. Enable the Paperless workflow from step 7. Send one more file with paperless-drop and
   confirm the whole chain runs with no clicks.

## 10. Two saved views, as a safety net

Not a queue. You never have to clear them.

- **AI processed (7 days):** *Tags: has `paperless-gpt-auto-complete`*, *Added: within the last
  week*. Enable "Show on dashboard". Use it for occasional spot checks.
- **AI needs attention:** *Tags: has `paperless-gpt-failed`*. Dashboard on. It should stay empty.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `401` / `403` in the logs | Wrong `PAPERLESS_GPT_TOKEN`, or the user lacks permission (step 2) |
| `404 model not found` | Check the slug at <https://openrouter.ai/models>. Keep `openai/gpt-6-luna` unless it has been retired. |
| `400` mentioning `temperature` | Some models reject temperature `0`. Add `VISION_LLM_TEMPERATURE: "1.0"` (the README documents this for GPT-5). |
| Documents get `paperless-gpt-failed` | See the log line just before it. After `AUTO_TAG_MAX_RETRIES` failures paperless-gpt gives up on that document (and stops billing for it). Fix the cause, remove `paperless-gpt-failed`, then re-add `ai-inbox`. |
| Documents stay on `ai-inbox` | The poll hasn't reached them, the workflow file has an error (check the logs), or the trigger tag name doesn't match the file. |
| Metadata is mediocre | Tune prompts in the paperless-gpt UI under **Settings**, using **Test on a document**. Changes apply immediately. |
| Costs higher than expected | Lower `OCR_LIMIT_PAGES`, and check that `OCR_SKIP_DIGITAL_PAGES` is `"true"`. |

**Emergency stop:** disable the Paperless workflow (step 7), or run
`docker compose stop paperless-gpt`. Documents already processed keep their changes, and the
document history in Paperless shows what changed.

## Optional: backfill old documents

Tag existing documents with `ai-inbox` in batches of 20–50 (bulk edit in the Paperless
document list), then watch the cost for the first batch before doing more.
