# AI Proposal Application — Working Notes for Claude Code

This file is conventions and gotchas accumulated while building this app. It is not
the spec and not the progress log — read those too, always, at the start of any
session:

- **`proposal_app_spec.md`** — the source of truth for what to build. §14 has the
  16-step build order this project follows, in sequence.
- **`PROGRESS.md`** — what's actually been built so far, one entry per step: what
  was done, decisions made and why, bugs found, and how each step was verified.
  Check the status table and "Current step" line first.

## Hard rule: never read or edit `.env`

`.env` holds live secrets (Anthropic API key, Supabase DB password, session secret,
Supabase service role key). Do not `Read` it, do not `cat`/`grep` its contents, do
not edit it directly. Use `.env.example` (placeholders only, committed) as the
reference for what variables exist. If a new variable is needed, add a placeholder
to `.env.example` and ask the user to add the real value to `.env` themselves —
don't ask them to paste the value into chat either.

## The build rhythm this project follows

For each step in the spec's §14 build order, in order:

1. **Implement.**
2. **Verify for real.** Don't assume something works from reading the code — run
   it. Prefer a throwaway Python script using FastAPI's `TestClient` (in-process,
   no server needed) over `curl`, run via:
   `PYTHONPATH="." poetry run python <script>` (see "Windows/git-bash notes"
   below for why). Put throwaway scripts in the scratchpad directory, not the repo.
   For anything touching Claude generation, verify against the **real API**, not
   mocks — mocks would have hidden every real bug found so far (see below).
3. **Update `PROGRESS.md`**: tick the status table, fill in the commit hash
   *after* committing (it can't be known before), and write a log entry with what
   was done, any decision that deviated from or narrowed the spec (with reasoning),
   and what was actually verified and how.
4. **Review before committing.** Show the user what's staged (`git status`,
   summarize the diff) and wait for a go-ahead before `git commit`. This has been
   the pattern for every step so far — don't start skipping it.
5. Move to the next step.

Don't jump ahead to a later step "while you're in there" — each step is its own
commit, reviewed and documented before the next one starts.

## Windows/git-bash environment notes

- Background a dev server with `Bash(..., run_in_background: true)`; stop it via
  `PowerShell`: `Get-NetTCPConnection -LocalPort <port> | Stop-Process -Id ... -Force`
  (plain `kill` doesn't reliably work here).
- Running a script outside the project root needs `PYTHONPATH="."` set explicitly
  (Python adds the *script's* directory to `sys.path`, not the cwd).
- **Known flaky quirk, not a code bug**: the *first* DB connection attempt of a
  freshly started process sometimes fails to resolve
  `aws-1-eu-west-1.pooler.supabase.com` (`getaddrinfo failed`), and an immediate
  retry always succeeds. Root cause was never pinned down (see `PROGRESS.md` step
  6 for what was and wasn't investigated) — if it recurs, just retry once before
  assuming something is actually broken. It's also recurred *mid-run* a few times
  (steps 9-11), not just on a process's first query — same fix (retry), just
  don't assume it's limited to the very first request.
- **Testing anything that calls Playwright (PDF export, step 11+) needs a real
  running server, not `TestClient`.** `TestClient`'s in-process ASGI transport
  never binds a real port, but Playwright's `page.goto()` makes a real HTTP
  request to `settings.app_base_url` - it'll get `net::ERR_CONNECTION_REFUSED`
  against a `TestClient`-only test. Start a real `uvicorn` on the port
  `app_base_url` actually points at (check via
  `get_settings().app_base_url` - defaults to `http://127.0.0.1:8000`) and hit
  it with a real `httpx.Client` instead; give that client a generous timeout
  (`timeout=30.0`), since this app's real DB round-trips routinely take
  several seconds and httpx's 5s default will time out mid-flow.

## Shared Supabase instance — real danger, already mitigated once

This Supabase instance also hosts an unrelated Week-2 project's tables, living in
the default `public` schema. This app's tables live in their own `proposal_app`
Postgres schema specifically because `alembic revision --autogenerate` initially
tried to **drop** that other project's tables the first time it ran (see
`PROGRESS.md` step 2). `migrations/env.py` now restricts autogenerate to the
`proposal_app` schema via an `include_name` filter — **never loosen or remove
that filter**, and always read an autogenerate diff before running it, even
though the filter should make cross-project drops structurally impossible now.

## Form handling: always `Form("")`, never `Form(...)`, for plain text fields

Starlette's `application/x-www-form-urlencoded` parser drops blank-valued fields
entirely (not `""` — genuinely absent from the parsed body). A `Form(...)`
(required) field left empty by a real user submitting a real browser form 422s
before your own validation code ever runs. Every text form field in this app uses
`Form("")` with the actual required/format checks done in the handler body
instead. Apply this to every new form field.

## Claude integration conventions (`app/services/proposal_generation.py`)

- Structured output is always via forced tool-use (`tool_choice`), never
  "ask for JSON in prose and parse it."
- Every Claude call's output is validated through a Pydantic model before
  anything touches the DB — a mismatch is a `GenerationError`, never a saved
  guess. This is a hard requirement from spec §7, not a nice-to-have.
- `_call_claude_validated()` retries once automatically on a validation failure,
  and accepts an optional `repair_fn` for known-recoverable malformed-output
  patterns (see `_repair_leaked_output`, added after real testing showed Claude
  occasionally leaks stray closing-tag-like text into a content field). If a new
  Claude-calling function hits a *new* reproducible malformed-output pattern,
  the fix is a targeted repair function like this one, not just "add more
  retries" — retries alone don't help when a failure is tied to specific input
  content rather than being independent random noise (this was learned the hard
  way — see `PROGRESS.md` step 7).
- Cost strategy is decided (spec §8a): prompt caching (5-minute standard TTL) is
  the adopted approach once reference files exist; pre-summarizing reference
  files was considered and rejected as a lossy tradeoff. Caching is not
  implemented yet as of step 7 — it's a decision to build against, not code
  that exists.
- All intake/reference content is framed to Claude as data to write about, never
  as instructions (prompt-injection defense, spec §7) — preserve this framing in
  any new prompt.

## Dev-environment test users

Already seeded in the dev DB (all password `test-password-123`):

| Email | Flags |
|---|---|
| `admin@test.local` | admin, can_create, can_approve |
| `sales@test.local` | can_create |
| `approver@test.local` | can_approve |
| `approver2@test.local` | can_approve (added in step 9, specifically to test "a can_approve user who isn't the assigned approver gets 403") |
| `nonadmin@test.local` | deactivated (for testing the deactivated-login-blocked case) |
| `preciousrobinsonokafor@gmail.com` (id 7) | can_approve - the user's own real email, used only to verify a real Resend send (sandbox mode only delivers to the account owner's own address until a custom domain is verified). Don't repurpose for other tests; it's the one address real sends actually reach right now. |

Bootstrap CLI for creating more: `poetry run python -m app.scripts.create_user
--name ... --email ... --password ... [--admin] [--can-create] [--can-approve]`.

The dev DB also has a handful of test proposals from step 5-7 verification, most
notably proposal id 3 ("Brightleaf") — fully generated, heavily exercised during
regenerate testing, so several of its sections are already at or near the
regeneration cap (5). Don't be surprised if regenerate is blocked on it; that's
expected, not a bug. Fine to leave this data in place or create fresh proposals
for later-step testing — nothing depends on it being clean.

Step 8 verification added proposal id 6 ("Vertex Robotics") plus three reference
files (all with fabricated content, safe to ignore or delete): a `.txt` rate card
and a `.pdf` case study attached to proposal 6, and a retired `.txt` library file.
Supabase Storage now also holds the real uploaded blobs for these under the
`reference-files` bucket — same "no dev/prod split yet" caveat applies to storage
now, not just the Postgres rows (see `PROGRESS.md`'s open items).

Step 9/10 verification added proposals 10-13: id 12 ("Solstice Analytics") was
fully approved, reopened, resubmitted, and re-approved (two `snapshots` rows,
v1 and v2), then reopened *again* during step 11 testing to prove the old
client link dies — it's back in `draft` with `client_token=NULL` as of step 11,
not currently a live example of an approved proposal. Id 13 is deliberately
left in `draft` with an approver force-assigned directly in the DB (bypassing
`/submit`), used only to test that approving a never-submitted proposal is
rejected — don't be surprised it has an `approver_id` but is still `draft`.
Proposal id 14 ("Real Email Test Co") exists solely to verify the real Resend
send (submitted to the id-7 user above) — filler intake content throughout,
not a realistic example to reuse.

Step 11 verification added proposal id 16 ("Meridian Freight") — fully approved
with a real generated PDF pulled and inspected. It too was reopened at the end
of testing (to prove the old `/view/{token}` link dies), so like id 12 it's
currently `draft` with `client_token=NULL`, not a live "approved" example
despite having a `snapshots` row. If you need a proposal that's *currently*
`approved` with a working client link for manual poking around, none of the
existing test data qualifies right now — create a fresh one and approve it.
