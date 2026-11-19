# Recap MCP — Design

Date: 2026-11-19
Status: draft for approval

## 1. Intent

The user is a brand ambassador who works short bar activations (BET program, mostly Bud Light/AB brands, Phoenix market). After each shift they must fill a recap in the Network Ninja "Staff App" (https://staff.betprogram.networkninja.com/): ~30 fields of counts, yes/no answers, spend, written feedback and photos. Doing it by thumb on a phone after three back-to-back bars is slow.

Goal: the user opens the **Claude mobile app**, talks (voice dictation), dumps everything about the shift in one go, and Claude fills the recap. Claude tells them what is still missing, asks follow-ups, reads the receipt photo for spend/tip, estimates crowd and swag numbers from their description, attaches photos, and leaves the recap as a **draft** in the Staff App. The user then opens the Staff App, reviews, and submits it themselves.

This is delivered as a remote **MCP server** the user adds to Claude as a custom connector.

## 2. Goals, non-goals, anti-goals

**Goals**
- List the shifts that need a recap; find one by bar name and/or date/time; ask the user when the match is ambiguous.
- Expose each recap's real fields (from the site's own recap definition), with which are required, which are visible given current answers, and which are still missing — answers and uploads.
- Accept answers in batches from Claude, validate them the way the site does, and always reply with what's left.
- Get photos from the user's phone into the right recap fields (setup, receipt, feature sign/branding) and the event's photo gallery (crowd photos).
- Show the receipt image to Claude so it can read pre-tip spend, tip, total and item quantities.
- Save the recap to the site as a **draft** (partial response).

**Non-goals**
- Final recap submission. The MCP never calls the final submit endpoint.
- Claims (pay/expense claims). Never read for action, never created, edited or deleted.
- OCR libraries or a second LLM API. Claude (in the user's chat) does all reading and estimating.
- Multi-user support. One user, one account.
- A web UI beyond the photo upload page.

**Anti-goals (would be a failure even if it "works")**
- Anything landing on the site as a submitted recap without the user pressing Submit themselves. (Crowd photos are the one deliberate exception. They go to the event gallery and are live as soon as they upload, which is how the user already handles them. User's decision, 2026-11-19.)
- Touching claims in any way.
- The password (or session token) committed to the repo, logged, or visible in tool output.
- An unauthenticated public URL that lets anyone fill or upload recaps as the user.
- Claude inventing numbers silently. Estimates are fine but must be told to the user as estimates.

## 3. Constraints

- **Client:** Claude mobile app (iOS) using a custom connector → the server must be reachable from the public internet over HTTPS (Anthropic's servers connect to it, not the phone).
- **Host:** the user's home Windows 10 PC, always on, with a tunnel. No paid hosting.
- **Language/stack (user's choice):** Python + Playwright.
- **Credentials:** must be encrypted/obscured; never in the codebase.
- **Photos per recap:** 1 setup, 1 receipt, optional 1 brand ad/feature sign, 5+ crowd photos. Taken on the user's iPhone.
- **Site behaviour is reverse-engineered**, not a documented API. It can change; failures must be loud, not silent.

## 4. What recon established (facts, 2026-11-17)

All from a read-only session against the real site. Saved samples live in the session scratchpad (not the repo; they contain other staff members' PII).

### 4.1 Auth
- Login page: username field → `POST /api/v1/auth/saml-request {"username", "browser_request": true}` → browser is redirected to an IdP page with a password field → back to the app with `saml_request_id` in the URL fragment → `POST /api/v1/auth/login-saml {"saml_request_id"}` returns `{"token": <JWT>, "staffing_users_id": 19985}`.
- JWT is used as `Authorization: Bearer <token>`. Its `exp` is **30 days** after issue. No 2FA observed.
- Playwright (headless Chromium) completed this flow unattended: fill `#username`, click "Log In", wait for `input[type=password]`, fill, Enter, wait for network idle, capture the token from the `login-saml` response.

### 4.2 Data endpoints (all `GET`, base `https://staff.betprogram.networkninja.com/api/v1/`)
Query string `Q = shifts_days_back=90&shifts_days_forward=null&shifts_exclude_unactionable=false&claims_days_back=90&claims_days_forward=null&claims_include_all_declined=true`
- `staffing_user/{uid}/positions?Q` — one row per shift the user is staffed on. Key fields: `id`, `events_id`, `staffing_users_id`, `recap_on_portal`, `start_date`/`start_time`.
- `staffing_user/{uid}/events?Q` — event details. Key fields: `id`, `venue_name`, `start_timestamp`, `end_timestamp`, `events_recaps_id`, `recaps_locked`, `events_statuses_name`, `street_address1`, `city`, `scheduling_fields[]` (brand = entry with `name == "Brand Family"` → `print_value`, e.g. "Bud Light"; `brands_name` is null).
- `recap_response_set/staffing_user/{uid}?Q` — submitted recaps (`events_id`, `status_id`, `form_responses[].field_responses[]`).
- `partial_responses/{uid}` — server-side drafts (empty at recon time).
- `recap_definition/{id}` — the form. Seen: `1` On Premise Recap, `2` Off Premise Recap, `4` No Recap, `7` Cancelled Event.
- `document/event?events_id=&staffing_users_id=` — the event's photo gallery (past shifts had 7–13 images each).

### 4.3 "Needs recap" rule
Mirrors the Staff App's own `Position.canRecap` plus "no response yet":
position start is in the past **and** `recap_on_portal == 't'` **and** event has `events_recaps_id` (and it is not `4`/"No Recap") **and** `recaps_locked != 't'` **and** event is not last-minute-cancelled (the app's `isLastMinuteCancelled`) **and** no submitted response exists for that `events_id`.
Events with a **draft** (partial response) still count; they are listed with status "draft saved" so the user can finish or submit them.
*My call:* the rule follows the Staff App's `Position.canRecap` exactly. That means events assigned the "Cancelled Event" form (def `7`) are recaps the user fills like any other. Recon had one: CARAMBA, Nov 13, status Finished. Last-minute-cancelled events are excluded because the app itself blocks recapping them.
Validated on real data: 71 past shifts with `recap_on_portal='t'` all have responses; 57 with `'f'` have none (someone else recaps those); 0 currently pending — matches the user's statement.

### 4.4 Recap definition shape
`pages[] → items[]` (forms/sections) `→ items[]` (field instances). Skip anything with `is_archived == 't'`.
Each field instance: `id` (the **instance id**, used everywhere below), `field.id` (= `events_custom_id`), `field.name`, `field.defined_as`, `field.options[]` (`id`, `name`, `active`; sometimes a dict, normalise to list), `validations[]`, `logic_groups[]`.
- **Required:** a validation with `validation_type == "responseRequired"`.
- **Cross-field:** `ltField` / `lteField` with `references_events_custom_instances_id`; `lteValue` with `value` (string, may have leading space).
- **Visibility:** `logic_groups[].conditions[]` with `logic` (seen: `EQUALS`), `value`, `references_events_custom_instances_id`. A field with logic groups is visible only when its conditions hold. Note: yes/no conditions compare against `"1"` while yes/no responses are stored as `"t"`/`"f"` — treat `t`≡`1`, `f`≡`0`. Hidden fields are not required.
- **Field types seen:** `FIELD_YES_NO` (`t`/`f`), `FIELD_TEXT`, `FIELD_TEXTAREA`, `FIELD_INTEGER`, `FIELD_FLOAT2`, `FIELD_MULTISELECT` (pipe-joined option ids, e.g. `4287|4289`), `FIELD_IMAGE_UPLOAD` (pipe-joined document ids), `FIELD_FORMULA` (computed by the site — never set). The full list of 49 types is at `GET recap_field_type`; unknown types must be reported, not guessed.

On Premise recap (def 1) fields, for orientation: manager check-in (Y/N + name), attendance, consumers engaged / sampled / purchased, swag count, trade spend Y/N → pre-tip spend + tip (+ receipt photo), setup photo, feature sign Y/N (+ photo), QR lanyard Y/N, bartender training Y/N, brand listed correctly Y/N (+ branding photo if No), five feedback textareas (positive, negative, account/bartender comments, BA highlights, BA opportunities), event successful Y/N + reason multiselects, "if other" text, optional Alchemer survey screenshot.

### 4.5 Write endpoints (from the Staff App JS bundle `bundle/staff_webapp/staff.<hash>.js`)
- **Recap photo:** `POST /api/v1/recap_response_set/upload?events_custom_id=<field.id>&events_id=<event>` multipart, file key `file` → `{id, uuid}`. The `id` goes into the field value (pipe-joined); the `uuid` into that field response's `document_uuids[]`.
- **Draft save:** `POST /api/v1/partial_recap_response_set` (class `PartialRecapResponseSaveTask`). Body is the serialized recap response (shape of `RecapResponseService.serialize`, which matches the submitted responses in 4.2: `events_id`, `events_recaps_id`, `staffing_users_id`, `form_responses[{events_custom_forms_id, field_responses[{events_custom_id, events_custom_instances_id, value, field_type, ...}]}]`). **Exact payload not yet observed on the wire** — see §7.
- **Final submit:** `POST /api/v1/recap_response_set` — **must never be called.**
- **Event gallery photo (crowd photos):** Add Photos modal → `POST /api/v1/document/save` (multipart), list via `GET document/event`, delete via `POST document/remove {ids}`. Form fields to be read from `DocumentService.save` / `AddPhotosModalController` in the bundle.
- Upload retry rule from the app: retry only on network failure, 503, or errors `IncompleteUpload|EmptyUpload|UploadStorageFailed`; **never retry a bare 500** (it may have stored the photo, a retry duplicates it).

## 5. The design (decided)

### 5.1 Shape
One Python process on the home PC:
- **MCP server** using the official `mcp` Python SDK (FastMCP), **Streamable HTTP** transport.
- **Photo upload page** served by the same ASGI app (Starlette, already a dependency of the SDK).
- **SQLite** file (`data/recaps.db`, stdlib `sqlite3`) for events, local answers and photo bookkeeping.
- **Photos on disk** under `data/recaps/<events_id>/<slot>/…`.
- **Site client:** plain HTTPS calls (`httpx`, already an SDK dependency) with the bearer token. Playwright is used **only** to log in.
- Exposed through a tunnel; started at Windows logon via Task Scheduler (server + tunnel).

### 5.2 Secrets
- Username and password stored in **Windows Credential Manager** via `keyring` (encrypted with the Windows login). A one-time CLI command sets them (`python -m recap_mcp set-credentials`, prompting with `getpass`). The code only reads them by service name.
- The JWT is also kept in keyring (it is a 30-day credential). Re-login when it is within 2 days of `exp` or any call returns 401.
- The connector secret (below) also lives in keyring or a git-ignored `.env`.
- `data/`, `.env`, and any recon dumps are git-ignored. Tokens/passwords are never logged or returned in tool output.
- The user should change the site password (it appeared in chat) and re-run `set-credentials`.

### 5.3 Protecting the public URL
- The MCP endpoint lives at `/<connector-secret>/mcp`, where the secret is ≥32 random URL-safe chars. That full URL is what the user pastes into the Claude connector settings. Requests to any other path get 404.
- Upload links are `/upload/<token>`: random token, bound to one `events_id`, expires 60 minutes after creation, stored in SQLite. Expired/unknown → 404.
- Upgrade path if this ever feels thin: OAuth on the connector. Not built now.

### 5.4 Tools (exposed to Claude)
Tool descriptions are part of the product: they tell Claude how to behave (ask follow-ups, mark estimates, never claim it submitted). Every tool that takes an event accepts `events_id` (returned by tool 1).

1. **`find_recaps(bar_name?, date?, time?, include_done=false)`**
   Refreshes positions/events/responses/partials from the site (at most once per 5 minutes), upserts into the `events` table, then:
   - no arguments → all shifts that need a recap (rule §4.3);
   - with arguments → fuzzy match on bar name (stdlib `difflib`, normalised: case, punctuation, "&"/"and") filtered by date/time.
   Each row: `events_id`, bar name, address/city, date, start time, brand, recap type (On/Off Premise/Cancelled), status (needs recap / draft saved / submitted).
   More than one match → return all and the description tells Claude to ask the user which one (mentioning the distinguishing detail: time, date, city).

2. **`get_recap_fields(events_id, only="all" | "missing" | "uploads")`**
   Loads the event's recap definition (cached per `id`+`version`), applies visibility logic against current local answers, and returns ordered fields: instance id, label, type, required, options (names), current value, and a status (`filled` / `missing` / `hidden`). Formula fields shown as read-only. `only="uploads"` returns just the image fields still empty (this is the user's "fields that still need an upload" tool). Always ends with a short summary: N required answers missing, M uploads missing.

3. **`fill_recap_fields(events_id, answers: {instance_id: value}, estimated: [instance_id]?)`**
   Batch, so one voice dump can fill many fields in one call. For each answer:
   - type check / coercion (yes/no words → `t`/`f`; integers; money to 2 decimals; multiselect option **names** → pipe-joined option ids; reject unknown options with the valid list);
   - site validations (`lteValue`, `ltField`, `lteField`, required non-empty), evaluated against the merged answer set;
   - refuses `FIELD_FORMULA` and `FIELD_IMAGE_UPLOAD` (photos come from the upload page).
   Valid answers are stored locally; invalid ones are returned with a reason and not stored. The response then includes the same "what's still missing" payload as `get_recap_fields(only="missing")` and tells Claude to read the missing items to the user.
   "Enough information" is Claude's judgment, steered by the description: only fill a field when the user said something that supports it; estimates (crowd size, engaged, sampled, swag) are allowed but must be passed in `estimated` and stated to the user as estimates; never pad text fields with invented feedback.

4. **`create_photo_upload_link(events_id)`**
   Creates the token (§5.3) and returns the URL plus what to upload. The page (plain HTML, no JS framework, phone-sized) has one file input per slot, all `accept="image/*"`:
   - **Setup** (1, required) → the definition's setup-photo field;
   - **Receipt** (1, required if trade spend = yes) → receipt field;
   - **Feature sign / brand ad** (0–1) → feature-sign field (or "branding in account" field when that one is the visible one);
   - **Crowd** (multiple, 5+ expected) → event photo gallery (`document/save`), not a recap field.
   Slot→field mapping is matched by keywords in field labels per definition (e.g. "set up"/"set-up", "receipt", "feature sign"/"display"/"branding"), kept in one small dict. A missing mapping is reported, never guessed.
   Uploads are saved to disk immediately (re-uploading a slot replaces it). The page shows thumbnails of what's already uploaded for each slot, with a remove button on each. When a crowd photo that has already gone to the site gallery is removed or replaced, the MCP deletes that copy from the site with `POST document/remove`. **It only ever deletes document ids it uploaded itself (recorded in `photos.site_document_id`), never anything else in the gallery.** This was the user's choice (option b, 2026-11-19). Images are normalised to JPEG and downscaled (long edge ~2000px) with Pillow so they're within the site's and Claude's image limits.

5. **`read_receipt(events_id)`**
   Returns the receipt image as MCP image content plus an instruction to read the **pre-tip subtotal**, **tip**, **total** and the **line items / quantities** (which inform "consumers sampled"), say them back to the user, and then call `fill_recap_fields` for the spend and tip fields. "Total Spend" is a formula field; the site computes it, and the tool notes if receipt total ≠ subtotal + tip so Claude can flag it. If no receipt was uploaded, it says so and offers the upload link.

6. **`save_recap_draft(events_id)`**
   1. Re-checks the event still needs a recap and is not locked. Then it fetches the site's current draft for the event, if there is one. If that draft changed since the MCP last saved it (the user edited it in the Staff App), **the site wins**. Its values are imported into local answers, nothing is saved, and Claude tells the user what changed. The site draft is also imported when an event is first opened and the MCP has no local answers yet. *My call:* edits on the phone are never silently overwritten.
   2. Uploads any not-yet-uploaded recap photos (`recap_response_set/upload`) and crowd photos (`document/save`), recording returned ids in SQLite so a retry never re-uploads (§4.5 retry rule).
   3. Builds the response payload from local answers + document ids and `POST`s **`partial_recap_response_set`** only.
   4. Returns: saved / failed (with the site's error text), plus any still-missing required fields.
   Allowed with missing fields (it's a draft). Tool description tells Claude: this does not submit; tell the user to open the Staff App, review and submit. Calling it again updates the same draft.

Hard rule in code: the site client has no function for `POST recap_response_set` (without `partial_`) or for any `claim` endpoint. Its delete function refuses any id that isn't in the `photos` table. A unit check asserts those paths appear nowhere in the source.

### 5.5 Local data
- `events(events_id PK, position_id, venue_name, address, city, start_ts, end_ts, brand, recap_def_id, recap_def_name, site_status, refreshed_at)`
- `answers(events_id, instance_id, value, estimated BOOL, updated_at, PK(events_id, instance_id))`
- `photos(id PK, events_id, slot, path, site_document_id NULL, site_document_uuid NULL, uploaded_at NULL)`
- `upload_links(token PK, events_id, expires_at)`
- `drafts(events_id PK, partial_id, saved_at, last_error)`
Recap definitions cached as JSON files under `data/defs/<id>-<version>.json`.

### 5.6 Errors
Site errors, auth failures, unknown field types and unmapped photo slots are returned to Claude as plain-language messages it can read to the user ("The site rejected the draft: <reason>"). A local `data/server.log` records requests and site errors, with tokens redacted.

### 5.7 Checks left behind
One `tests/test_core.py` (pytest or plain asserts) covering, against trimmed fixtures of the real definition JSON (def 1, no PII): needs-recap rule, visibility logic (t≡1), validations (`ltField`, `lteValue`), multiselect name→id, fuzzy bar matching with ambiguity, and the "no submit/claim endpoints in source" assertion.

## 6. Left to the builder
- Module layout (aim: a handful of files), tunnel product (see §8 my call), exact tool wording, HTML of the upload page, logging format, cache durations, how the Task Scheduler entries are created (a short README section is enough).

## 7. Open risks to verify on the first real recap
The first live test is the next shift that becomes recappable (Nov 21: Tacos & Craft 5:30 PM, then Game Time, LOLA), saved as a draft only:
1. **Draft payload shape** — derive from `PartialRecapResponseSaveTask` + `RecapResponseService.serialize` in the bundle; if uncertain, capture the Staff App's own draft save once in Playwright (start a recap in the real app, let it autosave, record the request, don't submit) and copy that shape.
2. **Draft shows up in the Staff App** — confirm the user sees it on their phone and can submit it. If drafts saved via API don't appear, stop and re-decide with the user (do **not** fall back to final submit).
3. **`document/save` form fields** for crowd photos.
4. Whether the "No Recap" definition (`4`) events ever appear with `recap_on_portal='t'`.

## 8. Calls I made (for the user to check)
- **One "find" tool** for both "list everything pending" and "find by bar/time", instead of two.
- **"Fields still needing upload"** is `get_recap_fields(only="uploads")` rather than a separate tool.
- **Added `save_recap_draft`** — nothing reaches the site until it's called; Claude calls it when the user says they're done (or asks).
- **Photos are labelled by slot on the upload page**, so the receipt tool doesn't have to guess which image is the receipt.
- **Crowd photos go to the event photo gallery** (Add Photos), because the recap form has no crowd-photo field.
- **Tunnel:** ngrok with its free static domain (stable URL, no domain purchase). If the user owns a domain, Cloudflare Tunnel instead.
- **Python libs:** `mcp`, `playwright`, `keyring`, `pillow`. Nothing else.
