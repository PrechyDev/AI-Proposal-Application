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

## Dev database: wiped clean as of 2026-09-09

Every table except `users` was fully truncated (`RESTART IDENTITY CASCADE`) at the
user's explicit request, once the full pre-deployment regression pass (see
`PROGRESS.md`'s dated entry) confirmed the app itself was working correctly - the
accumulated test proposals/sections/reference files/tokens from the entire build
were deliberately dropped, not lost by accident. `users` was reduced to exactly one
row: `preciousokafor280@gmail.com` (admin, `can_create`, `can_approve`) - every
other seeded test account (`admin@test.local`, `sales@test.local`,
`approver@test.local`, etc.) is gone. All of the specific-proposal-id narration
this section used to carry (which ones were approved/sent/reopened, which had fake
emails, etc.) no longer applies to anything in the live DB - don't go looking for
proposal id 12 or 18, they don't exist anymore.

**Supabase Storage was *not* wiped** - the `reference-files` bucket still holds
whatever blobs were uploaded during the build, now orphaned (no DB row points at
them anymore). Harmless (small, private bucket), but don't be surprised it's not
empty even though every `reference_files` row is gone.

Bootstrap CLI for creating more test users: `poetry run python -m app.scripts.create_user
--name ... --email ... --password ... [--admin] [--can-create] [--can-approve]`.
Every account created any other way (via `/admin/users`) is invite-only - no
password until the invitee completes `/accept-invite/{token}`.

## Invite-based user creation + forgot-password (post-step-16)

`users.password_hash` is now nullable - `None` means "invited, hasn't set a
password yet" (`app/models/tokens.py`'s `account_tokens` table backs both the
invite-link and forgot-password-code flows; see `PROGRESS.md`'s post-step-16
follow-up and `proposal_app_spec.md` §6a). Real-flow verification added several
throwaway `@example.invalid` test users (`invite.test.*`, `reset.test.*`,
`screenshot.invite.*`) - safe to ignore or delete, not meant as reusable
fixtures like the named `*@test.local` accounts above.
