# AI Proposal Application

A FastAPI + Postgres web app for Koya Talent's proposal workflow: a salesperson turns
discovery-call notes into a Claude-generated proposal, revises it section-by-section
in a Google-Docs-style workspace, routes it to an approver, and — once approved —
sends the client a secure, time-limited link to a read-only page they can view and
download as a PDF. Every step (generation, approval, delivery, client access) is
logged centrally, independent of link expiry, so there's always a full audit trail.

Read these two documents alongside this one, in this order:

- **[proposal_app_spec.md](proposal_app_spec.md)** — the full architecture reference
  and build spec: schema, personas, edge cases, every non-obvious decision and why
  it was made.
- **[PROGRESS.md](PROGRESS.md)** — the build log: what's been done, step by step,
  what was verified and how, every decision that deviated from the spec.

## Stack

- **Backend**: FastAPI (Python 3.11+), SQLAlchemy, Alembic migrations
- **Frontend**: Jinja2 templates + HTMX (no separate SPA build step)
- **Database**: Postgres via Supabase, isolated in its own `proposal_app` schema
- **File storage**: Supabase Storage (reference files)
- **AI**: Claude API (Anthropic), forced tool-use for structured output
- **Email**: Mailjet (primary), Gmail SMTP (fallback)
- **PDF export**: Playwright headless-browser print, against the same template the
  in-app preview uses

## Local setup

1. **Install dependencies** (Poetry):
   ```
   poetry install
   ```
2. **Copy the environment template** and fill in real values:
   ```
   cp .env.example .env
   ```
   See `.env.example` for what each variable is and where to find it (Supabase
   dashboard, Anthropic console, Mailjet/Gmail). Never commit `.env` — it holds live
   secrets.
3. **Run migrations**:
   ```
   poetry run alembic upgrade head
   ```
4. **Run the app**:
   ```
   poetry run uvicorn app.main:app --reload
   ```
   Visit `http://127.0.0.1:8000`. `GET /health` does a DB round-trip and reports
   `{"status":"ok","database":"connected"}` if everything's wired up correctly.
5. **Create your first user** (there's no public signup — every account is invited
   by an existing admin, except the very first one):
   ```
   poetry run python -m app.scripts.create_user --name "Your Name" --email you@example.com --password "..." --admin --can-create --can-approve
   ```
   Log in at `/login`. From there, use **Manage Users** to invite everyone else —
   they'll get an email with a link to set their own name and password.

## Roles

Three independent permission flags on each user (not mutually exclusive — a person
can hold any combination):

- **`can_create`** — build and submit proposals
- **`can_approve`** — review, comment on, and approve/reject proposals assigned to them
- **`is_admin`** — manage users, see and filter every proposal regardless of role,
  reassign approvers

## Deploying

See the deployment plan (shared separately) for the full step-by-step Render
walkthrough, including how to get a base URL without owning a custom domain yet.
In short: this repo already has a `Dockerfile` and `render.yaml` ready to go —
create a Render Web Service pointed at this repo, it auto-detects the Docker
runtime, and every secret in `render.yaml` is marked `sync: false` (set manually in
Render's dashboard, never committed).

## Project conventions

If you're picking this codebase up as an AI assistant (or a new engineer), also
read **[CLAUDE.md](CLAUDE.md)** — accumulated conventions and gotchas from building
this app that aren't part of the spec itself (env-file handling, Windows/git-bash
quirks, the Claude integration's structured-output conventions, and what dev/test
data currently exists in the database).
