# AI Proposal Application — Build Specification
**Project**: Week 3 deliverable — AI-powered proposal application for Koya Talent
**Purpose of this doc**: full architecture reference for your own review, and a build spec you can feed to Claude Code section by section.

---

## 1. Executive Summary

A FastAPI + Postgres web app where salespeople turn discovery-call notes into a Claude-generated proposal, revise it section-by-section, route it to a designated approver, and — once approved — send the client a secure, time-limited link to a read-only page they can view and download as a PDF. Every step is logged centrally, independent of link expiry, so the business always has a full audit trail of what was proposed, by whom, approved by whom, and whether the client ever opened it.

---

## 2. Architecture

**Stack**
- Backend: **FastAPI** (Python)
- Frontend: **Jinja2 templates + HTMX** — no separate SPA/build step; HTMX handles partial-page updates (e.g. regenerating one section) with plain backend endpoints
- Database: **Postgres** (e.g. Supabase, consistent with your Week 2 project)
- PDF export: **headless-browser print-to-PDF** (e.g. Playwright's PDF function) run against the *same* HTML template used for the in-app preview — this guarantees the preview a salesperson reviews is pixel-identical to what the client downloads, because there's only one template, not two parallel renderers
- Email delivery: transactional email API (e.g. Resend or SendGrid) for both approver notifications and client delivery
- AI: **Claude API** (model choice in §8)

**Why FastAPI + Jinja/HTMX over a separate React frontend**: your core interaction — edit a section, see it update, without disturbing the rest of the page — is exactly what HTMX partial-swaps are built for, with one backend and one template layer. A separate frontend would only pay off if you needed complex client-side state, which this doesn't.

**High-level component map**
```
[Salesperson/Approver Browser] --(login, HTMX)--> [FastAPI App] --> [Postgres]
                                                        |--> [Claude API] (generation/regeneration)
                                                        |--> [Email API] (approver notify, client send, 7-day nudge)
                                                        |--> [PDF renderer] (headless browser print)
[Client Browser] --(no login, /view/{token})--> [FastAPI App, read-only route] --> [Postgres] (log access only)
```

---

## 3. Data Layer (Postgres schema)

```
users
  id, name, email, password_hash,
  can_create (bool), can_approve (bool), is_admin (bool),
  is_active (bool)   -- soft-delete, never hard-delete a user with proposal history

proposals
  id, client_name, client_email, company_name, date_of_call,
  client_needs_summary, project_scope, goals_and_objectives,
  recommended_services, proposed_timeline, estimated_pricing,
  created_by (-> users.id), approver_id (-> users.id, nullable until submitted),
  status: draft | pending_approval | changes_requested | approved | sent
  client_token (uuid, nullable until approved), token_expires_at,
  first_opened_at (nullable), created_at, updated_at

sections
  id, proposal_id, section_key, content, sort_order,
  has_gap_marker (bool)   -- true if Claude flagged this section incomplete

section_history
  id, section_id, old_content, new_content,
  change_type: manual_edit | regenerate,
  triggering_comment (nullable), changed_by, created_at

approval_comments
  id, proposal_id, section_key, comment_text, created_by, created_at, resolved (bool)

snapshots
  id, proposal_id, version_number, full_content_json, created_at
  -- written once, at the moment of approval; immutable "what the client saw"

reference_files
  id, name, storage_path, tags[], is_library (bool), uploaded_by, created_at

proposal_references
  proposal_id, reference_file_id   -- join table; drives the "References used" list on the proposal

delivery_logs
  id, proposal_id, channel, status: success | failed, error_message, attempted_at

access_logs
  id, proposal_id, ip_address, user_agent, accessed_at
```

**Why this shape**: one `status` enum on `proposals` drives the whole state machine, so every test scenario (approval gate, delivery, failure) is a status transition you can assert on directly, rather than logic scattered across separate stage tables.

---

## 4. Pages / Screens

| Route | Who | Purpose |
|---|---|---|
| `/login` | internal users | auth |
| `/proposals` | salesperson/approver | dashboard, filtered by `created_by = me OR approver_id = me` |
| `/admin/proposals` | admin | all proposals, filterable (status, client, salesperson, approver, date) |
| `/admin/users` | admin | create/deactivate users, assign `can_create`/`can_approve` |
| `/proposals/new` | salesperson (can_create) | intake form, required fields enforced |
| `/proposals/{id}/edit` | salesperson | section-by-section review: edit inline, comment + regenerate, view section history |
| `/proposals/{id}/approve` | approver (can_approve) | same rendered preview, add comments per section, Approve / Request Changes |
| `/library` | can_create users | reusable reference file library — upload, tag, retire |
| `/view/{token}` | client, no login | read-only rendered proposal + Download PDF button; invalid/expired token → redirect to business homepage |

---

## 5. Central Logging / Audit Design

Independent of the client link's 30-day expiry — expiry only kills the *client's current access route*; the audit trail is permanent (or archived deliberately, never auto-purged with the link).

What's logged, and where:
- **Every content change** → `section_history` (who, what changed, manual edit vs. AI regenerate)
- **The approved version itself** → `snapshots` (frozen, immutable)
- **Every send attempt** → `delivery_logs` (success/failure + error)
- **Every client view** → `access_logs` (proves whether/when it was opened — this is also what powers the 7-day nudge)

Dashboard visibility is a query filter, not a separate permission table: salesperson sees `created_by = me`, approver sees `approver_id = me` (and can see who created it), admin sees everything with filters.

---

## 6. Flow per Persona

**Salesperson**
1. Fill intake form (required fields enforced by the form itself).
2. Optionally attach reference files (from library or new upload → prompted "add to library?").
3. Claude generates the draft; any field that was blank *or* filler text gets a gap marker in that section instead of a fabricated guess.
4. Review the rendered preview. Per section: edit manually, or leave a comment + regenerate (warns first if that section already has a manual edit, since regenerating would overwrite it).
5. Pick an approver from the `can_approve` list, submit.
6. If sent back with "changes requested," repeat step 4.
7. Once approved and sent, see delivery status and — after 7 days — a nudge notification if the client hasn't opened the link yet.

**Approver**
1. Get an email notification with a link back into the app (login required — the email itself carries no proposal data or shortcut access).
2. Review the same rendered preview as the salesperson.
3. Leave section comments if changes are needed → "Request Changes" (routes back to salesperson).
4. Or **Approve** — but only if no section still has an unresolved gap marker; approving is blocked until every gap is resolved.
5. On approve: snapshot frozen, client token generated, client email sent.

**Admin**
1. Creates/deactivates users, assigns `can_create`/`can_approve`.
2. Manages the reference library (tag, retire stale files).
3. Views all proposals with filters — status, client, salesperson, approver, date range.

**Client**
1. Receives an email with a `/view/{token}` link (no login).
2. Sees the proposal rendered read-only, with a Download PDF button that exports the exact same view.
3. Cannot navigate anywhere else in the app — any bad/expired token redirects to the business homepage, never to an internal page or error revealing the app's existence.
4. Their view is logged (timestamp, IP) — this is what triggers the salesperson's 7-day nudge if it never happens.

---

## 7. Edge Cases and How They're Solved

| Edge case | Solution |
|---|---|
| Required field left blank | Form validation blocks submission |
| Field filled with filler/placeholder text | Claude (not form validation) judges insufficiency at generation time and inserts a gap marker instead of fabricating |
| Approver tries to approve with unresolved gaps | Approve action is disabled/blocked until all gap markers are resolved |
| Regenerate clicked on a manually-edited section | Warn first ("this section has manual edits — overwrite?") before regenerating |
| Proposal edited after approval | Requires an explicit "reopen" action; reopening invalidates the old client token and issues a new one, so no live client link ever points at stale content |
| Approver's `can_approve` revoked mid-flight | Admin can reassign the pending proposal to a different approver |
| Claude API fails/times out | Intake data is saved *before* calling Claude, so a failed generation never loses the salesperson's input — just retry |
| Claude returns malformed output | Validate response shape before writing to `sections`; on mismatch, surface "generation failed, retry" rather than saving garbage |
| Regenerate spam (cost control) | Soft cap on regenerations per section |
| Prompt injection via client input or uploaded files | System prompt treats all intake/reference content strictly as data to summarize, never as instructions |
| PDF export fails | Failure surfaces on the proposal record; "approve" cannot silently succeed while the export failed behind it |
| Email API is down | Logged to `delivery_logs` with the error; proposal shows a clear "delivery failed, retry" state, never silently marked `sent` |
| Duplicate send (double-click / retry) | Idempotency check — only send if status isn't already `sent` |
| User account deleted but has proposal history | Soft-delete (`is_active = false`) — never hard-delete, so historical `created_by`/`approved_by` records stay intact |
| Client link found/guessed by a random person | 128-bit unguessable UUID token (same model as DocuSign/Drive share links) + 30-day expiry + access logging — no email-confirmation gate, to keep client friction low |
| Proposal sent but never opened | 7-day check (via `access_logs`) triggers a notification back to the salesperson to send a manual nudge — the tool's job stops there; no in-app negotiation loop |
| Two people editing the same proposal simultaneously | Out of scope for this build — documented as a known limitation, not silently unhandled |

---

## 8. Claude Model Selection

**Recommendation: `claude-sonnet-5`** for all generation and regeneration calls.

**Criteria used**:
- **Output nature**: this is long-form, persuasive, client-facing prose that has to follow a fixed template structure and produce clean, parseable per-section output (JSON keyed by section) — needs strong instruction-following, not just raw fluency.
- **Judgment required**: the model has to decide, per field, whether the input is substantive enough to write from or should be gap-marked — that's a reasoning task, not a lookup, which rules out the cheapest/smallest tier.
- **Context needs**: must ingest the full intake, the template, and potentially multiple uploaded reference documents in one call — needs a model with comfortable context headroom, not a model optimized purely for short exchanges.
- **Volume vs. quality tradeoff**: this app generates proposals occasionally, not at high QPS — the cost difference between a mid-tier and top-tier model on a handful of calls per day is trivial next to the cost of a proposal that reads poorly to a paying client. Quality wins over shaving pennies here.
- **Latency tolerance**: a salesperson clicking "generate" can tolerate a few seconds; there's no real-time constraint pushing toward the fastest/cheapest model.

A single model across all calls keeps the build simple for this scope — no need to split generation and "gap-judgment" into separate model tiers.

### Model comparison

Cost assumption: one proposal = 1 initial generation call (~3,000 input / ~1,500 output tokens) + ~2 section regenerations (~1,500 input / ~300 output tokens each) ≈ **6,000 input tokens and 2,100 output tokens per proposal**. Actual usage will vary with reference-file size and how often a salesperson regenerates — this is a reasonable mid-case estimate, not a guarantee. Rates below are Anthropic's current official API pricing.

| Criterion | Haiku 4.5 | Sonnet 5 | Opus 5 |
|---|---|---|---|
| Long-form persuasive prose quality | Adequate, but tends toward generic/flatter phrasing | Strong — this is its sweet spot | Strongest, but the gain over Sonnet is marginal for this genre of writing |
| Instruction-following (fixed template, structured section-key output, gap-marking judgment) | Reliable for simple structure, weaker at nuanced judgment calls | Reliably follows structure and makes the filler-vs-real judgment call well | Best judgment quality, but overkill precision for a binary-ish call |
| Context handling (intake + template + reference docs) | Fine for this volume | Comfortable headroom | Comfortable headroom, no meaningful edge here |
| Rate per MTok (input / output) | $1 / $5 | $2 / $10 | $5 / $25 |
| **Est. cost per proposal** | **~$0.017** | **~$0.033** | **~$0.083** |
| **Est. cost per 100 proposals** | **~$1.65** | **~$3.30** | **~$8.25** |
| Latency | Fastest | Fast enough | Slowest of the three, still workable |
| Where it actually matters here | Good fit only for a cheap, low-stakes sub-task | Matches every requirement of this job | Solves a problem this job doesn't have |

At this volume, the entire Opus-vs-Sonnet cost gap is about $5 per 100 proposals — not large enough to be the deciding factor either way. The Sonnet 5 recommendation above is driven by quality/judgment fit, not cost avoidance; model spend here is a rounding error next to the value of one well-written proposal landing a client.

---

## 9. Deployment Decisions

- **Hosting**: a platform with minimal ops overhead given the timeline — e.g. Railway or Render for the FastAPI app (both support Postgres add-ons and env-based secrets easily), rather than self-managing a VM.
- **Database**: Supabase Postgres (reuses infra you already know from Week 2) or the hosting platform's managed Postgres — either is fine; Supabase is worth it only if you also want its storage bucket for `reference_files` uploads.
- **File storage**: reference files and generated PDFs should go to object storage (Supabase Storage or S3-compatible), not the app's local disk — local disk doesn't survive redeploys on most PaaS platforms.
- **Secrets**: Claude API key, email API key, DB URL — all as environment variables, never committed.
- **Domain for client links**: a short, clean subdomain (e.g. `proposals.yourcompany.com`) reads more trustworthy in a client's inbox than a raw platform-generated URL.

---

## 10. Decisions Made — Full Log (explicit and implicit)

**Explicit (you chose directly)**
- Backend: FastAPI
- Document approach: web preview that renders exactly as the PDF, downloadable as that same PDF
- Approval flow: comment-per-section → targeted regenerate → in-app approval → shareable link
- Database: Postgres
- Role model: capability-based (`can_create`/`can_approve`), self-approval allowed
- Client access: unguessable UUID token, 30-day expiry, access logging, no email-confirmation gate
- Visibility: creator/approver see their own; admin sees all with filters
- Versioning: section-level history + frozen approval snapshot
- Regenerate-conflict handling: warn before overwriting manual edits
- Reference material: reusable library + per-proposal upload, listed as visible references, cited directly in text when relevant
- Approver notification: email with a link back into the app (login required)
- Intake validation: required fields compulsory on the form; filler text is Claude's job to catch, not the form's
- Approval gate: blocked while unresolved gap markers exist
- Reopen behavior: invalidates old client token, issues a new one
- Scope boundary: tool's job ends at delivery/download, no negotiation loop
- Feature: 7-day unopened-link nudge notification to the salesperson
- Client link expiry: 30 days

**Implicit (followed from the above, not separately discussed)**
- Soft-delete for users (not hard-delete), so historical audit records never break
- Idempotent email sending, to prevent duplicate client sends on retry/double-click
- Save-draft-before-AI-call ordering, so a Claude failure never loses salesperson input
- Bad/expired client tokens redirect to the business homepage, never to any internal-looking page or error — the app's existence shouldn't be discoverable externally
- A single Claude model across generation and regeneration, rather than a multi-model pipeline, to match this project's scope
- One `status` enum driving proposal state, rather than separate tables per stage

---

## 11. Questions We'd Ask a Real Client (with reasoning)

1. **How many proposals a month, and how many salespeople/approvers?** — decides whether this stack's scale is even appropriate, or whether they need something heavier.
2. **Does this need to sync into an existing CRM (HubSpot/Salesforce), or is this the system of record on its own?** — avoids building a parallel, disconnected source of truth.
3. **Is there a locked brand template (logo, fonts, colors) the PDF must match exactly?** — changes whether this is a structure-only build or a full design-system build.
4. **Does approval need to scale by deal size** (e.g. anything over $X needs a second sign-off)? — the single-approver model may be too thin for larger contracts.
5. **Any compliance requirement (GDPR, data residency) on where client PII and proposal content are stored?** — directly affects hosting/region choice, not just a "nice to know."
6. **Does "sent" mean the tool's job is done, or do they want negotiation/outcome tracking (won/lost) inside the same system?** — you already scoped this out, but a real client may expect it, so it's worth confirming explicitly rather than assuming.
7. **Is pricing always freeform text from the salesperson, or should it eventually pull from a rate card/calculator?** — changes the estimated_pricing field from free text to a structured calculation down the line.
8. **What should happen if a client wants to negotiate/ask a question directly from the proposal page** — even without a full negotiation loop, do they expect a "Reply" or "Ask a question" affordance on the client view, or is email entirely outside the tool?
9. **How sensitive is the content in a typical proposal** — is a leaked link (even unguessable + expiring) a real liability, or is this generally non-sensitive information? — this is what would tip the email-confirmation-gate decision the other way for a specific client.

---

## 12. Testing Plan (mapped to the PRD's 7 required scenarios)

| # | Scenario | How to verify |
|---|---|---|
| 1 | Normal generation | Submit a fully-filled intake form → confirm all sections render coherently, no gap markers |
| 2 | Missing information | Submit with a required field left as filler text (e.g. "n/a") → confirm that section shows a gap marker, not fabricated content |
| 3 | Supporting material | Attach a reference file → confirm it appears in the proposal's "References used" list and is directly cited in relevant section text |
| 4 | Section regeneration | Manually edit one section, regenerate a different section → confirm the manual edit is untouched and `section_history` shows both changes separately |
| 5 | Human approval | Attempt to access the client link before approval → confirm it doesn't exist yet; approve → confirm the link now works |
| 6 | Final delivery and logging | Approve and send → confirm `delivery_logs` shows success, client can view/download, and the proposal shows in the central dashboard |
| 7 | Failure handling | Simulate an email API failure (e.g. bad API key temporarily) → confirm `delivery_logs` shows the failure clearly and the proposal is not marked `sent` |

**Additional tests worth running given your added edge cases**
- Try to approve a proposal with an unresolved gap marker → confirm it's blocked
- Regenerate a section that has manual edits → confirm the overwrite warning appears
- Reopen an approved proposal → confirm the old client link stops working and a new one is issued
- Let a client link sit unopened for the test equivalent of 7 days (fake the timestamp in dev) → confirm the salesperson gets notified
- Hit `/view/{badtoken}` → confirm it redirects to the homepage, not an error page

---

## 13. What You Need to Do Yourself (not buildable by Claude Code alone)

- Create accounts and get API keys: Anthropic (Claude API), email provider (Resend/SendGrid), hosting platform, Supabase (or chosen Postgres host)
- Decide and register the domain/subdomain for client-facing links
- Write 2–3 realistic sample intake inputs (including one with intentionally filler/missing fields) to use as your test data and for the "generated proposal sample" deliverable
- Create your own test user accounts for each role (admin, salesperson, approver) once the app is deployed
- Record the Loom walkthrough after the core flow works end-to-end
- Fill in the testing evidence table using the scenarios in §12
- Write the one-page documentation (this spec can be condensed down for that)
- Fill in the reflection sheet, including the model-selection reasoning from §8

---

## 14. Suggested Build Order (for feeding to Claude Code step by step)

1. Project scaffold: FastAPI app structure, Postgres connection, env config
2. Schema migration for all tables in §3
3. Auth + user model (login, `can_create`/`can_approve`/`is_admin` checks)
4. Admin: user management pages
5. Intake form + validation (required fields)
6. Claude integration: generation call, section-key JSON parsing, gap-marker logic
7. Proposal edit page: manual edit, comment + regenerate (with overwrite warning), section history
8. Reference library: upload, tag, attach to proposal, "used as reference" display
9. Approval flow: submit → approver queue → email notification → approve/request-changes → gap-marker gate
10. Snapshot freeze + client token generation on approval
11. `/view/{token}` client-facing page + PDF export (headless browser print)
12. Client email delivery + `delivery_logs`
13. Access logging on `/view/{token}` + 7-day unopened nudge job
14. Central dashboard with role-filtered visibility + admin filters
15. Failure-state polish across generation/PDF/email (surfacing errors clearly per §7)
16. Deployment + domain setup