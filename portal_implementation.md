# Proxy Portal — Implementation Guide

**Audience:** whoever (human or model) implements `portal/`. Assumes no memory of the design
conversation. Read this end-to-end before writing code.

**Status:** Phase 0 (library refactor) is **complete and tested — 85 tests pass**. Everything
described here is Phase 1+ and does not exist yet.

---

## 1. What this is

A private, invite-gated web portal where ~15–30 friends submit Magic decklists for a shared
proxy-printing order, get them validated and auto-fixed, see what their share costs, and pay via
PayPal **before** printing starts — so the organiser (Fabian) stops fronting the money for the
whole run.

The heavy PDF pipeline (`moxfield_mpc_to_pdf.py`: Scryfall images, bleed, Real-ESRGAN upscaling,
reportlab) **stays on Fabian's Mac**. The server only handles text, JSON and bookkeeping.

Non-commercial. Must not read as a shop. German UI. EU/GDPR-conform.

### Scale reality check

27 lists, ~3000 card lines, ~15 people per round. Rounds run ~2 weeks every ~2 months. **Every
decision below is sized for that**, not for growth. Resist adding infrastructure.

---

## 2. Non-negotiables

Violating any of these produces a system that takes money and can't deliver.

1. **Never let an unvalidated list look clean.** If Scryfall is unreachable, the check FAILS.
   It never returns a partial result. (`ScryfallUnavailable` already enforces this — do not
   catch and swallow it.)
2. **One Moxfield parser.** `proxy_parser/moxfield_line.py` is the only place that parses a
   card line. If the portal counted cards differently from the pipeline, people would pay for
   cards that never print. Never re-implement the regex.
3. **Money in integer cents.** Never float. `0.13 * 100` eventually renders `13.000000000002`.
4. **Payments are append-only records**, not a boolean flag. Partial payments, overpayments and
   refunds must all be representable.
5. **Price snapshots are immutable.** Changing a rate never re-prices an existing order.
6. **Uploads never touch the filesystem** and the client filename is never used as a path.
7. **No torch, reportlab, Pillow, numpy or scipy in the web image.** If `requirements-web.txt`
   grows one of those, something is architecturally wrong.
8. **No third-party CDN, fonts, or analytics.** Self-host everything. This is a GDPR
   requirement and is what lets the site run without a cookie banner.
9. **`input/` and `input_final/` are git-ignored** — their filenames contain real people's
   names. Never commit them, never copy them into the image.

---

## 3. Architecture

```
┌──────── Hetzner CX22, Nürnberg (DE) — only while a round is open ────────┐
│  Caddy (auto-TLS, :80/:443)                                              │
│      └─> FastAPI + Jinja2 + HTMX   (uvicorn --workers 1)                 │
│              ├── imports proxy_parser.checker  (in-process, no RPC)      │
│              ├── SQLite WAL  /data/portal.db   (one file to move)        │
│              └── one background job thread (checks, serialized)          │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │ HTTPS, bearer token, pull-only
                    ┌────────────┴────────────┐
                    │ worker/print_worker.py  │  Fabian's Mac (Phase 2)
                    │ writes ready/*.txt      │
                    │ runs moxfield_mpc_to_pdf.py --input-dir ready
                    │ 22 GB caches, MPS, 15 GB PDFs — all local
                    └─────────────────────────┘
```

**Why the server stays tiny:** the checker fetches Scryfall **JSON only**. The 5.2 GB
`scryfall_cache/` on the Mac comes from `scryfall_client.py` downloading PNGs, which never runs
server-side. Server metadata cache for ~1800 unique cards ≈ **20 MB**.

### Stack decisions (settled — do not revisit)

| Choice | Why |
| --- | --- |
| FastAPI + Jinja2 + HTMX | The checker is Python and imports directly. HTMX covers the three interactive needs (poll check progress, accept fixes, drop a card) with **no JS build step**. |
| SQLite WAL + SQLAlchemy 2.0 + Alembic | Single node, ≤50 users. The DB is one file you `scp` between VPS incarnations — exactly the spin-up/teardown model. Alembic from day one: ad-hoc `ALTER TABLE` on payment records loses data. |
| No Celery/Redis | A `job` table + one thread. Scryfall calls are blocking and **must** be serialized for rate limiting anyway. |
| Caddy | Automatic Let's Encrypt across repeated server rebuilds. |
| Pico.css, self-hosted | No CDN (GDPR). System fonts. |

---

## 4. Layout

```
mtg_proxy_parser/
├── proxy_parser/                 # ✅ EXISTS — import, do not fork
│   ├── moxfield_line.py          #    the single line parser
│   └── checker/                  #    classify_upload, parse_deck_lines,
│                                 #    evaluate_traffic_light, render_fixed_lines,
│                                 #    ScryfallBatchValidator, ScryfallCache
├── portal/                       # 🔧 BUILD THIS
│   ├── __init__.py
│   ├── main.py                   # app factory, middleware, startup/shutdown
│   ├── settings.py               # pydantic-settings, reads .env
│   ├── db.py                     # engine, session, SQLite pragmas
│   ├── models.py                 # SQLAlchemy models
│   ├── migrations/               # alembic
│   ├── security.py               # hashing, tokens, CSRF
│   ├── auth.py                   # gate, sessions, dependencies
│   ├── ratelimit.py              # token buckets in SQLite
│   ├── checking.py               # THE bridge to proxy_parser
│   ├── pricing.py                # cents arithmetic
│   ├── jobs.py                   # background runner
│   ├── db_cache.py               # ScryfallCache backed by SQLite
│   ├── slugs.py                  # person_slug, reference codes
│   ├── notify.py                 # pluggable mail (default: none)
│   ├── cli.py                    # admin bootstrap, retention
│   ├── routes/
│   │   ├── public.py  account.py  lists.py  orders.py
│   │   ├── admin.py   gdpr.py     worker.py
│   ├── templates/                # Jinja2, German
│   └── static/                   # pico.css, htmx.min.js — vendored
├── worker/print_worker.py        # 🔧 Phase 2
└── deploy/                       # 🔧 Dockerfile.web, docker-compose.yml,
                                  #    Caddyfile, backup.sh, restore.sh
```

---

## 5. Configuration

`portal/settings.py` — pydantic-settings, reads `deploy/.env`.

```python
class Settings(BaseSettings):
    secret_key: str                      # signs cookies; startup FAILS if empty/default
    worker_token: str                    # bearer token for the Mac worker
    admin_password: str                  # bootstrap admin login
    invite_code: str                     # shared site gate
    paypal_me_handle: str
    portal_domain: str = "localhost"
    database_url: str = "sqlite:////data/portal.db"
    scryfall_cache_ttl_days: int = 30
    retention_days: int = 90
    mail_backend: str = "none"           # none | smtp | brevo
    debug: bool = False
```

**Startup assertion:** refuse to boot if `secret_key` is empty, `"changeme"`, or shorter than
32 chars. A silently-default signing key is a silent authentication bypass.

Operational settings that Fabian edits at runtime (prices, PayPal handle, invite code) live in
the **`settings` DB table**, not here. `.env` holds only bootstrap secrets.

---

## 6. Data model

All timestamps UTC. All money `Integer` cents. `ON DELETE` explicit.

```python
# ---- configuration -------------------------------------------------------
class Setting(Base):                      # typed key/value singleton store
    key: str (PK); value: str; updated_at; updated_by_user_id
    # cost_per_card_cents=13, cost_per_list_cents=26, processing_charge_cents=0,
    # paypal_me_handle, invite_code_hash, retention_days, intro_text_md

class Round(Base):
    id; code: str (unique)                # "2611"
    name; state: str                      # DRAFT|OPEN|CLOSED|IN_PRINT|COMPLETED|ARCHIVED
    opens_at; closes_at; created_at
    # rates SNAPSHOTTED when the round opens:
    cost_per_card_cents; cost_per_list_cents; processing_charge_cents
    max_cards: int | None                 # soft capacity cap

# ---- identity ------------------------------------------------------------
class User(Base):
    id; display_name: str
    person_slug: str (unique)             # ^[A-Za-z]+$ — see §9
    email: str | None                     # OPTIONAL, opt-in notifications only
    notify: bool = False
    access_code_hash: str                 # argon2
    is_admin: bool; is_blocked: bool
    created_at; last_seen_at
    privacy_accepted_at; privacy_version: str | None

class LoginToken(Base):                   # personal invite link
    id; user_id (FK); token_hash (unique) # sha256 of the raw token
    purpose: str                          # INVITE | LOGIN
    created_at; expires_at; used_at | None

class Session(Base):
    id; user_id (FK); token_hash (unique) # sha256; the cookie holds the raw token
    created_at; last_seen_at; expires_at; revoked_at | None

# ---- submissions ---------------------------------------------------------
class Submission(Base):
    id; round_id (FK); user_id (FK)
    title: str                            # user-chosen deck name, "Glarb cEDH"
    ordinal: int                          # 1..n within (round, user)
    state: str                            # see §7
    current_version_id | None; accepted_version_id | None
    created_at; updated_at; withdrawn_at | None

class SubmissionVersion(Base):            # IMMUTABLE
    id; submission_id (FK); version_no: int
    source_kind: str                      # PASTE | TXT | PDF
    original_filename: str | None
    raw_text: str                         # exactly as submitted (LF-normalised)
    content_sha256: str
    created_at
    __table_args__ = (UniqueConstraint("submission_id", "version_no"),)

class CheckRun(Base):
    id; submission_version_id (FK)
    state: str                            # QUEUED|RUNNING|DONE|FAILED
    traffic_light: str | None             # GREEN|YELLOW|RED — NULL while running
    scryfall_ok: bool                     # False => degraded, never orderable
    total_cards: int; unique_lines: int
    audit_json: str                       # serialised FileAuditResult
    fixed_text: str | None; fixed_sha256: str | None
    started_at; finished_at; error: str | None
    checker_version: str                  # git sha, for reproducibility

# ---- money ---------------------------------------------------------------
class Order(Base):                        # one per (round, user, seq)
    id; round_id (FK); user_id (FK); seq: int
    state: str                            # see §7
    reference_code: str (unique)          # "MTG-2611-CHRIS-K7Q2"
    price_snapshot_id | None
    locked_at; paid_confirmed_at; frozen_at | None
    created_at; updated_at
    __table_args__ = (UniqueConstraint("round_id", "user_id", "seq"),)

class PriceSnapshot(Base):                # IMMUTABLE — what the user agreed to
    id; order_id (FK)
    cost_per_card_cents; cost_per_list_cents; processing_charge_cents
    n_cards: int; n_lists: int; total_cents: int
    breakdown_json: str                   # [{submission_id, title, cards, sha256}]
    created_at

class OrderSubmission(Base):
    order_id (FK, PK); submission_id (FK, PK); submission_version_id (FK)
    n_cards: int

class PaymentRecord(Base):                # APPEND-ONLY
    id; order_id (FK)
    amount_cents: int                     # may be partial; NEGATIVE for refunds
    kind: str                             # PAYPAL | CASH | ADJUSTMENT | REFUND
    received_on: date; note: str | None
    recorded_by_user_id (FK); created_at

# ---- jobs & audit --------------------------------------------------------
class Job(Base):                          # in-process check queue
    id; kind: str                         # CHECK
    submission_version_id | None
    state: str                            # QUEUED|RUNNING|DONE|FAILED
    attempts: int = 0
    created_at; started_at; finished_at; error | None

class PrintJob(Base):                     # Phase 2
    id; round_id (FK); state: str
    payload_sha256: str
    claimed_by: str | None; claim_token_hash: str | None
    lease_expires_at; heartbeat_at | None
    progress_pct: int; progress_message: str | None
    result_json: str | None               # pdf names/sizes/counts — NEVER files
    created_at; started_at; finished_at; error | None

class AuditLog(Base):
    id; at; actor_user_id | None; actor_kind: str   # USER|ADMIN|WORKER|SYSTEM
    action: str; subject_type: str; subject_id: str
    detail_json: str | None; ip_hash: str | None

class ScryfallCacheRow(Base):
    namespace: str (PK); key: str (PK)
    payload: str; fetched_at; expires_at

class RateLimitBucket(Base):
    key: str (PK); tokens: float; updated_at
```

### Why prices are snapshotted twice

Once onto `Round` when it opens, again onto `PriceSnapshot` per order. The first stops a
mid-round rate edit from re-pricing everyone; the second pins exactly what one person agreed to.
Both are needed.

### Why `scryfall_ok` exists

Because of the bug fixed in Phase 0 (see §2.1). A check that couldn't reach Scryfall must be
incapable of unlocking an order, and that has to be a stored fact, not an inference.

---

## 7. State machines

These are authoritative. Implement transitions as explicit guarded functions, not `if` chains
scattered across routes.

### Submission

```
DRAFT ─submit→ CHECKING ─error→ CHECK_FAILED ─retry→ CHECKING
                  │
   ┌──────────────┼──────────────┐
   ▼ GREEN        ▼ YELLOW       ▼ RED
 CLEAN       NEEDS_REVIEW     NEEDS_FIXES ─edit / drop card→ CHECKING
   │              │ accept fixes      │
   └────────→ ACCEPTED ←──────────────┘
                  │ order locked
                  ▼
              LOCKED ─order frozen→ FROZEN ─job claimed→ IN_PRINT_RUN → DONE
                  │
        admin unlock / user withdraw → ACCEPTED / WITHDRAWN
```

Rules:

- **Any edit creates a new `SubmissionVersion` and forces a re-check.** `accepted_version_id`
  is cleared. If the order was merely `LOCKED` (not paid), it drops to `OPEN` and its
  `PriceSnapshot` is voided.
- `FROZEN` refuses edits outright. Only an admin unlock reverses it, and it is audit-logged.
- `NEEDS_FIXES` (RED) blocks the whole order — no price is shown.
- Acceptance stores `fixed_sha256`. That hash is the anti-drift anchor the Phase-2 worker
  verifies before printing.

### Order

```
OPEN ─all submissions ACCEPTED, user confirms→ LOCKED
  ▲                                              │ admin records payment
  │ admin unlock / user edits a list             ▼
  └────────────────────────── PARTIALLY_PAID ──(sum ≥ total)──→ PAID
                                                                  │
                                            FROZEN → IN_PRINT_RUN → COMPLETED
                                                                  │
                                    negative PaymentRecord → REFUNDED
     any state ─admin, with reason→ CANCELLED
```

- `LOCKED` computes the `PriceSnapshot` and generates the `reference_code`.
- `PAID` is set by **Fabian clicking a button**, never by an API.
- Balance is always `SUM(PaymentRecord.amount_cents)` vs `PriceSnapshot.total_cents` — never a
  stored field.
- Adding a list after paying creates a **new Order** with `seq=2`. A paid order is never mutated.

---

## 8. The checker bridge — `portal/checking.py`

The **only** module that touches `proxy_parser`. Exact signatures (verified against the code):

```python
classify_upload(file_name: str, file_size: int, file_path: str = "",
                file_type: str | None = None) -> FileMetadata
parse_deck_lines(lines: Sequence[str], stated_count: int | None = None) -> DeckContentReport
ScryfallBatchValidator(cache_dir: str = "scryfall_cache",
                       cache: ScryfallCache | None = None)
    .validate_cards(cards: list[ParsedCardLine]) -> list[CardDiagnostic]   # RAISES ScryfallUnavailable
evaluate_traffic_light(meta, content, diagnostics) -> FileAuditResult
render_fixed_lines(audit: FileAuditResult) -> tuple[list[str], list[str]]  # (lines, changes)
```

```python
def run_check(raw_text: str, user: User, submission: Submission,
              cache: ScryfallCache) -> CheckOutcome:
    # 1. Synthesize FileMetadata WITHOUT the filesystem.
    meta = classify_upload(
        file_name=f"{user.person_slug}{submission.ordinal}_{slug(submission.title)}.txt",
        file_size=len(raw_text.encode("utf-8")),
        file_type="PLAIN_TEXT",
    )
    meta.person = user.display_name   # authoritative: the account, not the filename
    meta.is_valid_filename = True     # filenames are server-generated
    meta.stated_count = None          # no count-in-filename on the web

    # 2. Pure parse.
    content = parse_deck_lines(raw_text.splitlines(), stated_count=None)

    # 3. Scryfall. Let ScryfallUnavailable propagate — the job marks FAILED.
    diagnostics = ScryfallBatchValidator(cache=cache).validate_cards(content.cards)

    # 4. Unchanged verdict logic.
    audit = evaluate_traffic_light(meta, content, diagnostics)

    # 5. Pure renderer.
    fixed_lines, changes = render_fixed_lines(audit)
    return CheckOutcome(audit=audit, content=content,
                        fixed_text="\n".join(fixed_lines) + "\n", changes=changes)
```

### Two CLI behaviours to suppress

Both are filesystem concepts with no web meaning. **Suppress them by feeding a clean
`FileMetadata`, never by editing `evaluate_traffic_light`** — that function must stay shared
with the CLI:

| Behaviour | How it's suppressed |
| --- | --- |
| "Auto-rename file" YELLOW reason | `meta.is_valid_filename = True`, `filename_issues = []` |
| `stated_count` mismatch | `stated_count=None` |

### Dataclasses you'll serialise

```
FileAuditResult: metadata, content_report, card_diagnostics, traffic_light,
                 status_summary, can_auto_fix, auto_fix_actions,
                 manual_fix_actions, applied_changes
CardDiagnostic:  card_name, line_number, set_code, collector_number, severity,
                 category, message, can_autofix, suggested_fix, scryfall_uri
DeckContentReport: total_lines, total_cards_count, unique_card_lines, cards,
                 unparseable_lines, section_headers, duplicate_cards,
                 missing_set_codes_count, count_mismatch, count_discrepancy_msg,
                 content_issues, can_autofix_content
```

All plain dataclasses → `dataclasses.asdict()` into `audit_json`. `card_diagnostics` is what the
UI renders per line.

Diagnostic categories: `NOT_FOUND`(RED), `DIGITAL_ONLY`(RED), `PRINTING_NOT_FOUND`(YELLOW),
`DIGITAL_PRINTING`(YELLOW), `FOIL_ONLY`(YELLOW), `OVERSIZED`(YELLOW), `LOW_RES`(INFO),
`DOUBLE_FACED`(INFO).

### `portal/db_cache.py`

Implement the `ScryfallCache` protocol against `ScryfallCacheRow`:

```python
class SqliteScryfallCache:
    def get(self, namespace: str, key: str) -> dict | None   # honour expires_at
    def put(self, namespace: str, key: str, payload: dict) -> None
```

Namespaces (from `proxy_parser/checker/scryfall_cache.py`): `PRINTING` (`"{set}_{cn}"`),
`NAME`, `NONFOIL`, `PAPER`. TTL 30 days for hits, 24 h for misses (a card may simply be
unreleased yet).

**This table must survive server teardown** — it ships inside the backed-up DB file, so each
round starts with a warm cache.

### Input handling

- **Primary input is a big textarea** ("Moxfield-Export hier einfügen"). Most friction vanishes.
- `.txt` accepted (256 KB cap).
- `.pdf` best-effort via the existing pypdf path: 2 MB, 20 pages, **subprocess with a 10 s
  timeout**, and `< 10` parseable lines → RED with "bitte den Text einfügen".
- `.docx` **rejected** with a friendly message. Do not add another zip parser to a
  network-facing service for a handful of users.

Normalise every input: UTF-8 with `errors="replace"`, strip BOM (real submissions have them —
see `input_final/Baris_300.txt`), CRLF→LF, strip NULs, cap 2000 lines / 500 chars per line.

---

## 9. Slugs and codes — `portal/slugs.py`

### `person_slug` (this one has a sharp edge)

The print pipeline derives the person and the divider-card label **from the filename**, via
`deck_parser.parse_filename`, whose person regex is `^([^\W\d_]+)` — letters only, stopping at
the first digit, space or underscore.

So the worker's filenames must carry exactly one letters-only prefix.

```python
def make_person_slug(display_name: str, taken: set[str]) -> str:
    """NFKD ASCII-fold, letters only. 'Simon Münchberg' -> 'SimonMuenchberg'."""
    # umlauts: ä->ae, ö->oe, ü->ue, ß->ss BEFORE NFKD, else Münchberg -> Munchberg
    # collisions resolve with a LETTER suffix: Chris, ChrisB, ChrisC
    # A DIGIT suffix would silently break parse_filename. Enforce ^[A-Za-z]+$ in the DB.
```

This also retires the pre-existing `"Jan David Wirtz" → "Jan"` bug by construction.

Worker filename: `{person_slug}{ordinal}_{title_slug}.txt` → `Chris4_GlarbCEDH.txt`, which
round-trips exactly through `parse_filename`. **Add a test for this.**

### `reference_code`

`MTG-{round_code}-{PERSON_SLUG_UPPER}-{4 chars}` using Crockford base32 minus `I O U 1 0`
(people transcribe these by hand into PayPal).

---

## 10. Pricing — `portal/pricing.py`

```python
def compute_total(n_cards: int, n_lists: int, rates: Rates) -> int:
    return (n_cards * rates.cost_per_card_cents
            + n_lists * rates.cost_per_list_cents
            + rates.processing_charge_cents)
```

Card count comes from the **accepted, fixed** list (`content.total_cards_count` after auto-fix),
so what's billed is exactly what prints.

State the rule publicly — someone will ask: **per card copy** (`sum(qty)`), a double-faced card
counts once, the per-list charge covers the divider card.

German display uses a comma decimal separator and a `Kostenübersicht` heading — never
`Rechnung`, no invoice number, no VAT line.

```
103 Karten × 0,13 €     = 13,39 €
  2 Listen × 0,26 €     =  0,52 €
  Bearbeitungspauschale =  0,00 €
  ─────────────────────────────
  Gesamt                = 13,91 €
  Verwendungszweck: MTG-2611-CHRIS-K7Q2
```

---

## 11. Auth & security

### Two independent layers

1. **Invite gate** — one shared code, argon2id-hashed in `settings`, constant-time verify.
   Sets a `gate` cookie (30 d). Checked by middleware before *anything* renders.
   5 attempts / 15 min / IP, then a 1 h lockout. Generic error message.
   `robots.txt` + `X-Robots-Tag: noindex`.
2. **Personal access code** — 10-char Crockford base32, argon2-hashed, one per participant,
   regenerable by admin. Redeeming sets a session.

Middleware order: `gate → session → CSRF → route`.
**Exempt from the gate:** `/api/worker/*`, `/healthz`, `/datenschutz`.

### Sessions

32-byte random token; store **only the sha256**. Cookie holds the raw value.
`HttpOnly; Secure; SameSite=Lax; Path=/`. 60-day sliding expiry, rotated on login.
Server-side revocable (`revoked_at`) → real logout and "überall abmelden".
**Not JWT** — unrevocable tokens buy nothing here.

### CSRF

Double-submit token in a hidden field, plus `hx-headers='{"X-CSRF-Token": "..."}'` on `<body>`.
All mutations POST. Worker endpoints exempt (bearer-authed, not cookie-authed).

### Rate limits (token buckets in SQLite)

| Scope | Limit |
| --- | --- |
| gate attempts | 5 / 15 min / IP |
| submission create | 20 / h / user |
| re-check | 10 / h / user |
| data export | 1 / h / user |
| worker poll | 60 / min / token |
| global | 300 / min / IP (at Caddy) |

### Upload safety

Extension allowlist **plus content sniffing**. Caps enforced in Caddy *and* the handler.
**Bytes go straight into `SubmissionVersion.raw_text`** — never to disk, and the client filename
is never used as a path. That eliminates path traversal outright.

### Secrets

`.env`, mode 0600, never in the image or git. Admin bootstrap via
`python -m portal.cli promote <name>` inside the container — never a web route.

---

## 12. Background jobs — `portal/jobs.py`

One `threading.Thread` started at app startup, draining the `job` table.

```python
def run_worker_loop(stop_event):
    while not stop_event.is_set():
        job = claim_next_job()          # UPDATE ... WHERE state='QUEUED' + rowcount
        if not job:
            stop_event.wait(1.0); continue
        try:
            execute(job)
        except ScryfallUnavailable as ex:
            fail_job(job, ex, retry=job.attempts < 3)   # backoff 1m/5m/15m
        except Exception as ex:
            fail_job(job, ex, retry=False)
```

**At startup, requeue every job stuck in `RUNNING`** — the thread dies with the process, and a
forgotten job leaves a submission spinning forever. One line; easy to forget.

Serialized by design: one check at a time is both the Scryfall rate-limit story and ample
throughput (a cold 100-card list ≈ 2–5 s; a whole cold round ≈ 20 s of API time).

The UI polls `GET /lists/{id}/status` with `hx-trigger="every 2s"` while `CHECKING`.

---

## 13. Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/gate` | invite-code form |
| GET | `/` | round status, your lists, your order |
| GET | `/login/{token}` | redeem a personal invite link |
| POST | `/logout` | revoke session |
| GET | `/datenschutz` | privacy notice (gate-exempt) |
| GET/POST | `/lists/new` | paste or upload |
| GET | `/lists/{id}` | check result: traffic light, diagnostics, diff |
| GET | `/lists/{id}/status` | HTMX poll fragment |
| POST | `/lists/{id}/accept` | accept auto-fixes → ACCEPTED |
| POST | `/lists/{id}/edit` | new version → re-check |
| POST | `/lists/{id}/drop-card` | remove one offending line → re-check |
| POST | `/lists/{id}/withdraw` | WITHDRAWN |
| POST | `/order/confirm` | lock → snapshot + reference code |
| GET | `/order` | Kostenübersicht + PayPal link |
| POST | `/order/mark-sent` | "Ich habe überwiesen" |
| GET/POST | `/account` | display name, optional email, notify opt-in |
| GET | `/account/export` | GDPR JSON export |
| POST | `/account/delete` | GDPR erasure (confirm step) |
| — | `/admin/*` | see §14 |
| — | `/api/worker/*` | see §15 |
| GET | `/healthz` | liveness (gate-exempt) |

---

## 14. Admin surface

- **Dashboard** — round state, submission counts by state, money table (expected / received /
  outstanding), failed checks, print-job status.
- **Rounds** — create / open / close / in-print / complete. Opening snapshots the rates.
- **Settings** — the three rates with a live *"eine 100-Karten-Liste kostet 13,26 €"* preview,
  PayPal handle, invite code (rotate), retention days, intro text.
- **People** — display name, `person_slug` (editable), lists, order state, amounts. Issue a new
  invite link; export; delete.
- **Orders (the money screen)** — reference code, itemised snapshot, payment history, *record
  payment* form (amount prefilled, editable; date; kind; note), unlock, cancel, freeze.
- **Lists** — every version, the check report (reuse the *same* Jinja partial users see), a
  unified diff of raw vs fixed, manual traffic-light override **with a mandatory reason**,
  re-check.
- **Exports** — round ZIP: the ready `.txt` files + `deck_check_report.md` (reuse
  `generate_markdown_report`) + totals CSV. **This is the Phase-1 escape hatch**: before the
  worker exists, Fabian downloads this and runs the pipeline by hand exactly as today.
- **GDPR** — retention dry-run then execute; audit-log viewer.

---

## 15. Worker protocol (Phase 2)

Pull-only. The server never connects to the Mac. Auth: one 32-byte bearer token, stored hashed,
constant-time compare.

| Endpoint | Behaviour |
| --- | --- |
| `GET /api/worker/jobs/next` | 204, or atomically `PENDING→CLAIMED`, returns `claim_token` + 30 min lease |
| `GET /api/worker/jobs/{id}/bundle` | `[{person_slug, ordinal, title, filename, n_cards, sha256, text}]` |
| `POST /api/worker/jobs/{id}/heartbeat` | extends lease; carries `progress_pct` + message |
| `POST /api/worker/jobs/{id}/complete` | `Idempotency-Key`; replay returns the original response |
| `POST /api/worker/jobs/{id}/fail` | same |

- Lease expiry (Mac sleeps / worker crashes) reverts the job to `PENDING`. A stale worker's
  `complete` with an old `claim_token` gets **409** and exits.
- **Idempotency is required, not optional** — the pipeline runs for hours and the Mac's network
  will flap.
- One non-terminal job per round (partial unique index).

Worker sequence:

1. Verify each list's sha256 against the bundle; mismatch → refuse the whole job.
2. Write `~/mtg_rounds/{round}/ready/{person_slug}{ordinal}_{title}.txt` (fresh dir).
3. **Pre-flight:** re-run the checker locally with `--no-scryfall`; assert every list parses and
   the card totals match the bundle. Cheap, and it catches drift *before* a night of GPU time.
4. `python3 moxfield_mpc_to_pdf.py --input-dir <dir> --mode upscaled --device auto --break-by name`
   — **invoked unchanged**; `--input-dir` is already a CLI flag (`proxy_parser/config.py:168`).
5. Stream stdout as progress; parse `output/card_totals_summary.csv` for the result.
   **Exit code 4 means unprintable cards** — surface that as a job failure with the card list.

---

## 16. GDPR requirements

Fabian is the **controller**. Do not rely on the household exemption.

| Data | Basis | Retention |
| --- | --- | --- |
| Display name + `person_slug` | 6(1)(b) contract | until deletion / 12 mo inactive |
| Email (optional) | 6(1)(a) consent | same |
| Decklists | 6(1)(b) | 90 days after round COMPLETED |
| Payment reference + amounts | 6(1)(b) | 90 days |
| Session/token hashes | 6(1)(f) | expiry + 7 days |
| Hashed/truncated IP | 6(1)(f) | 30 days |

**Not collected:** postal address, legal name, phone, IBAN, card data.

Must implement:

- `/datenschutz` in German, Art. 13-complete: controller + contact, the table above, Hetzner as
  processor (AVV signed), no non-EU transfer, all data-subject rights, complaint authority
  (**LDI NRW** for Cologne), no automated decision-making, version stored in
  `user.privacy_version` with acceptance timestamp.
- Say explicitly that **PayPal is outside the portal** — Fabian only records received/not, no
  payment data flows through, PayPal is its own controller.
- Say that **only card names** go to Scryfall — **no user identifier ever appears in a Scryfall
  request, including the User-Agent**.
- Exactly two strictly-necessary cookies (gate + session) → **no consent banner**; a footer line
  suffices.
- `/account/export` and `/account/delete` (cascade; orders mid-print pseudonymised to
  `deleted-user-<id>` keeping only amounts — and the UI must say so before confirming).
- Daily retention task, dry-run + admin execute in Phase 1.
- Caddy logs with IPs truncated (`/24`, `/48`), 7-day rotation. App logs carry `user_id`, never
  an email address or list content.

### Copy discipline

**"Kostenbeteiligung"** — never *kaufen*, *Warenkorb*, *Checkout*, *Shop*, *Rechnung*. Footer:

> Private, nicht-kommerzielle Sammelbestellung unter Bekannten. Keine Gewinnerzielungsabsicht —
> es werden ausschließlich die tatsächlichen Druckkosten umgelegt.

This is a copy problem, not an architecture problem, which is exactly why it's easy to break by
accident. Review the German strings once, deliberately.

---

## 17. Deployment

`deploy/docker-compose.yml`: **caddy** (80/443) + **web** (uvicorn `--workers 1` — SQLite and
the job thread want one process) + a periodic **backup** container (`sqlite3 .backup` → `age` →
prune).

`Dockerfile.web`: `python:3.13-slim`, `requirements-web.txt` only, copy `proxy_parser/` +
`portal/`, entrypoint runs `alembic upgrade head` then uvicorn. Non-root, read-only rootfs except
`/data`.

SQLite pragmas at connect: `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`,
`foreign_keys=ON`.

Caddy: HSTS, `nosniff`, `Referrer-Policy: no-referrer`, CSP
`default-src 'self'; img-src 'self' data:` — trivially satisfiable with HTMX self-hosted and no
inline JS.

Operational runbooks live in [`deploy/SETUP.md`](deploy/SETUP.md) and
[`deploy/ACCOUNT-SETUP.md`](deploy/ACCOUNT-SETUP.md).

---

## 18. Tests

Non-negotiable coverage:

| Test | Why |
| --- | --- |
| **Checker parity** — the 27 lists in `input_final/` through `portal.checking.run_check()` reproduce `deck_checker.py`'s verdicts | proves portal and CLI agree |
| **Outage safety** — mock 429/500/timeout; assert `CheckRun.state=FAILED`, `scryfall_ok=False`, and that **no** submission becomes GREEN or orderable | the highest-severity failure mode |
| **Pricing** — hand-computed cents incl. 0 cards, 1 list, non-zero fee; balance across partial + excess + refund | money |
| **`person_slug`** — `"Simon Münchberg"`, `"Jan David Wirtz"`, forced Chris/Chris collision all round-trip through `deck_parser.parse_filename` | the sharp edge in §9 |
| **State machine** — every illegal transition rejected; edit-after-lock voids the snapshot; edit-after-paid refused | correctness |
| **Security** — unauthenticated access to every route redirects to the gate; size/type/line caps trip; rate limiter trips | — |
| **GDPR** — delete a test user, assert every table clean but the anonymised audit row | legal |
| **Regression** — `tests/test_pipeline.py`, `test_checker.py`, `test_moxfield_line.py`, `test_scryfall_resilience.py` still pass | `proxy_parser/` must stay working |

End-to-end (local `docker compose up`): gate → login → paste the real
`input/Chris4_glarbDC.txt` (99 cards) → check → accept → amount reads
**99 × 0,13 + 1 × 0,26 = 13,13 €** → mark sent → admin confirms → frozen, edits refused.

---

## 19. Build order

Ship Phase 1 fully before starting Phase 2. **At the end of Phase 1 the system is already
useful**: Fabian downloads the round ZIP and runs the pipeline by hand, exactly as today, but
everyone has paid up front — which is the entire point.

**Phase 1** (~4–6 days)
1. `portal/settings.py`, `db.py`, `models.py`, first Alembic migration.
2. `security.py`, `auth.py`, gate + invite links + sessions.
3. `slugs.py` + tests (do this early — `person_slug` is on the critical path).
4. `db_cache.py`, `checking.py`, `jobs.py` + the parity and outage tests.
5. Submit → check → result page → accept fixes.
6. `pricing.py` → order confirm → snapshot → reference → PayPal link.
7. Admin: settings, people, orders, record payment, **round ZIP export**.
8. `/datenschutz`, export, delete, retention dry-run.
9. `deploy/` + a full local `docker compose` rehearsal.

**Phase 2** — worker API + `print_worker.py` + admin job dashboard.
**Phase 3** — optional opt-in email (Scaleway TEM or Brevo — both French/EU; **never**
Postmark/SendGrid/SES-us, and **never** Postfix on the VPS: Hetzner blocks outbound 25 and a
fresh IP has no reputation). `.pdf` upload. Retention automation.
**Phase 4** — round archive, scripted spin-up/down, metrics.

---

## 20. Known pitfalls

1. **PayPal.me cannot carry a reference code.** People will pay with no note at all. Admin must
   be able to match by name + amount + date. The reference helps; it is not a mechanism.
2. **PayPal.me amounts are editable by the payer.** The link is a suggestion. This is exactly
   why payments are append-only records rather than a boolean.
3. **Post-payment pipeline failure** (image 404, oversized layout) needs a refund path and one
   sentence of policy in the FAQ.
4. **The job thread dies with the process** — requeue `RUNNING` at startup.
5. **`person_slug` digit suffix silently breaks `parse_filename`.** Enforce letters-only in the
   DB constraint, not just in the helper.
6. **Do not edit `evaluate_traffic_light`** to suit the web. Feed it clean metadata instead; it
   is shared with the CLI.
7. **Do not catch `ScryfallUnavailable` and continue.** It exists precisely to stop a degraded
   run from looking clean.
8. **Scope creep** — sheet-packing and cross-person dedup were explicitly excluded. The model
   supports adding them later; don't.
9. **`input_final/` holds real names.** Confirm `.gitignore` before the first commit.

---

## 21. Phase 0 reference — what already exists

Completed and tested. Do not redo.

| Change | Location |
| --- | --- |
| Single Moxfield parser | `proxy_parser/moxfield_line.py` |
| `parse_deck_lines` (pure) | `checker/content_validator.py` |
| `classify_upload`, `cross_validate` | `checker/file_validator.py` |
| `render_fixed_lines` (pure) + **working duplicate consolidation** | `checker/autofix.py` |
| `ScryfallUnavailable`, retry/backoff, namespaced lazy cache | `checker/scryfall_validator.py`, `checker/scryfall_cache.py` |
| Unprintable-card reporting, exit code 4 | `moxfield_mpc_to_pdf.py` |
| Exit code 3 on Scryfall outage | `deck_checker.py` |
| Golden parser test (29 lists), outage tests | `tests/test_moxfield_line.py`, `tests/test_scryfall_resilience.py` |

Verified: **3467 cards billed = 3467 cards printed** across the 27 archived lists; 85 tests pass.
