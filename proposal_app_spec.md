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
- Email delivery: transactional email API — **Mailjet** (see §8c for why, over the originally-considered Resend/SendGrid, and over Brevo which this app also briefly used before its account was suspended) — for approver notifications, client delivery, and changes-requested notifications (§8d)
- AI: **Claude API** (model choice in §8)

**Why FastAPI + Jinja/HTMX over a separate React frontend**: your core interaction — edit a section, see it update, without disturbing the rest of the page — is exactly what HTMX partial-swaps are built for, with one backend and one template layer. A separate frontend would only pay off if you needed complex client-side state, which this doesn't.

**High-level component map**
```
[Salesperson/Approver Browser] --(login, HTMX)--> [FastAPI App] --> [Postgres]
                                                        |--> [Claude API] (generation/regeneration)
                                                        |--> [Email API] (approver notify, client send, changes requested)
                                                        |--> [PDF renderer] (headless browser print)
[Client Browser] --(no login, /view/{token})--> [FastAPI App, read-only route] --> [Postgres] (log access only)
```
(The 7-day nudge is a live dashboard query read by the salesperson's own browser, not an Email API call - see §9's "Background jobs" note.)

### 2a. HTMX: Retrofitted for the Section-Edit/Regenerate Interaction Only

**Status: gap found and closed post-step-16.** Despite §2 naming HTMX as core architecture from the start, an audit after step 16 (prompted directly by the user asking "was HTMX used?") found it had never actually been built anywhere across all 16 steps - every interactive action was a plain `<form>` POST followed by a full-page redirect. This was never flagged as a deliberate deviation in `PROGRESS.md`; it was simply never done.

**Decision on closing it: targeted, not a full retrofit.** Asked the user directly which they wanted; given no strong preference, applied the recommendation - retrofit exactly the one interaction this section's own rationale names ("edit a section, see it update, without disturbing the rest of the page"), not every form in the app. The manual-edit and regenerate forms for each section carry both a plain `action`/`method` (unchanged fallback if JS is disabled or fails to load) and `hx-post`/`hx-target`/`hx-swap` attributes targeting just that section's own fragment. The route handlers check an `HX-Request` header and return either the one section's re-rendered fragment (HTMX) or the original full-page redirect (plain form) - covering manual edit, regeneration, the regeneration-limit error, and the manual-edit-overwrite confirmation, all scoped to just that section. Simpler actions elsewhere in the app (admin filters, reference attach/remove) deliberately stay plain full-page forms - revisit only if a real need for more partial-swap interactions comes up.

**Correction (§6b): the htmx.org script itself was never actually loaded on any page until the §6b redesign.** This section originally (incorrectly) stated that the relevant page loaded htmx.js from a CDN - that never happened; no script tag referencing htmx existed anywhere in the app through the end of step 16 and into the initial stages of the §6b redesign. Every "async" section edit had, in every real browser, been silently falling back to the plain full-page-reload path this whole time (safely, since that fallback exists precisely for a missing/disabled htmx - but not the intended behavior). Caught only once §6b was verified with a real Playwright browser session instead of just `TestClient` (which sets the `HX-Request` header directly and so never exercises whether a browser would actually send it). Fixed by self-hosting a pinned `htmx.org` build rather than a CDN reference, now loaded on the unified workspace page (§6b) that carries every `hx-*` interaction in the app.

### 2b. Visual Design System (chosen for the email templates; not yet applied to the rest of the app)

**Status: chosen post-step-16, applied only to the three transactional email templates so far.** A second audit finding was that all three notification emails (approver, client delivery, changes-requested) were raw, unstyled inline-HTML f-strings - no real template file, no branding. Building real ones required an actual color/typography system, and neither this repo nor the original project brief folder had one anywhere (no logo, no brand colors). Asked the user directly; told to choose one and start with the emails, with the same system meant to be applied to the rest of the app's pages in a later pass (not part of this one).

**Chosen:**
- Primary/heading color: `#1F2D3D` (deep slate navy)
- Accent/CTA color: `#B8863B` (muted gold)
- Body text: `#3A4655`; muted/footer text: `#6B7280`
- Page background: `#F7F5F2` (warm off-white) behind a white content card
- Typography: `Georgia, 'Times New Roman', serif` for the "Koya Talent" wordmark/headings, `Arial, Helvetica, sans-serif` for body copy - deliberately no webfonts, since email clients strip `@font-face`/external stylesheets unreliably; a plain system-font stack renders consistently everywhere an email might be opened.

Implemented in `app/templates/emails/_base.html` (shared layout - single-column, 560px, table-based markup with every style inline, per email-client HTML conventions) and the three templates that extend it.

**Now also rolling out to the app's own pages, one at a time - `login.html` first, then `dashboard.html`.** A shared stylesheet (`app/static/styles.css`, served via a new `/static` mount in `main.py`) carries the same tokens (colors, fonts) as CSS custom properties plus reusable component classes (`.card`, `.field`, `.btn`, `.navbar`, `.stat-card`, `.status-pill`, etc.), so later pages can reuse it directly rather than redefining the palette. One new token was added along the way: `--color-success` (`#3F7D5C`, a muted sage green) - needed once the dashboard's proposal-status pills required a genuinely distinct "good/ready" meaning that none of navy/gold/muted/danger could represent without overloading an existing color. A shared `app/static/app.js` also now provides sitewide navigation feedback (a top progress bar on any link click or form submit - real page loads in this app routinely take a couple of real seconds, and clicks looked like they'd done nothing without it), included via the shared `_navbar.html` partial. **Since §6b, the rollout also covers every proposal-related page:** the unified workspace page, the role-scoped `/proposals` list (including the new `.tab-bar` component), and `admin_proposals.html` (previously bare unstyled HTML, restyled as part of that redesign since it needed the navbar anyway). **`/view/{token}` (the client-facing proposal document itself, and the literal source Playwright prints to PDF) was redesigned separately, post-§6b, on direct feedback that it "looks ugly"** - it stayed bare `<h1>`/`<h2>`/`<p>` HTML the longest of any page, since the internal-tool rollout above never touched a client-facing route. Rebuilt with the same brand tokens as the transactional emails (`app/templates/emails/_base.html`'s navy/gold/serif palette), as fully self-contained inline CSS rather than a link to `/static/styles.css` - matching the email templates' own precedent, since it must render identically wherever Playwright's headless Chromium fetches it. Same fix round also caught and fixed a real generation bug: Claude occasionally opened a section with a heading repeating that section's own title (already rendered separately by the template) - fixed both at the source (an explicit system-prompt instruction, §8e's forced-tool-use content still isn't 100% compliance-guaranteed) and defensively at render time (`strip_duplicate_heading()` in `app/templating.py`, so already-generated proposals are fixed too, not just future ones). Remaining unstyled pages, if any, are bare HTML until their own turn comes.

---

## 3. Data Layer (Postgres schema)

**Note**: "schema" here means table shape, not the Postgres namespace. All tables below live inside a dedicated Postgres schema named `proposal_app` (created via migration), not the default `public` schema — this Supabase instance is shared with other Koya projects (see §9), and namespacing by Postgres schema keeps this app's tables isolated from theirs at the database level, not just by naming convention.

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
  retired_client_token (uuid, nullable)   -- see §6b: the most recently-retired client_token, for the reopen messaging exception
  is_regenerating (bool)   -- see §6b: shared lock while a full-document regenerate runs in the background
  first_opened_at (nullable), created_at, updated_at

sections
  id, proposal_id, section_key, content, sort_order,
  has_gap_marker (bool)   -- true if Claude flagged this section incomplete

section_history
  id, section_id, old_content, new_content,
  change_type: manual_edit | regenerate | full_regenerate,
  triggering_comment (nullable), changed_by, created_at

approval_comments
  id, proposal_id, section_key (nullable - null means a whole-document comment, see §6b), comment_text, created_by, created_at, resolved (bool)

snapshots
  id, proposal_id, version_number, full_content_json, created_at
  -- written once, at the moment of approval; immutable "what the client saw"

reference_files
  id, name, description (nullable), storage_path, tags[], is_library (bool), uploaded_by, created_at,
  deleted_at (nullable)   -- trash marker (see §14a); NULL = live, set = pending the 30-day auto-purge

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
| `/proposals` | salesperson/approver | list of proposals scoped to `created_by = me OR approver_id = me`, with tabs (All / Created / Awaiting My Approval — see §6b) |
| `/admin/proposals` | admin | all proposals, filterable (status, client, salesperson, approver, date) |
| `/admin/users` | admin | create/deactivate users, assign `can_create`/`can_approve` |
| `/proposals/new` | salesperson (can_create) | intake form, required fields enforced. Since §6c: attach up to 5 reference files (library or device upload) right here, then one "Create Proposal" click creates the proposal, attaches everything, and generates - no separate later steps |
| `/proposals/{id}` | creator, assigned approver, or admin | **the one unified workspace page — see §6b.** Role-aware controls render on the same page based on the viewer's relationship to the proposal (`can_edit`/`can_review`, independently, not mutually exclusive): section-by-section edit/regenerate with inline Google-Docs-style comments, a "Raw Inputs" tab (creator/admin only), and Approve/Request Changes (assigned approver or admin). Replaces the original separate `/edit` and `/approve` pages below. |
| ~~`/proposals/{id}/edit`~~ | — | **Superseded by `/proposals/{id}` (§6b).** Kept only as a 303 redirect, since real emails sent before the redesign link here. |
| ~~`/proposals/{id}/approve`~~ | — | **Superseded by `/proposals/{id}` (§6b).** Kept only as a 303 redirect, for the same reason. |
| `/library` | can_create users | reusable reference file library — multi-file upload with name/description, preview, and a real Trash (30-day recovery, since §6c - replaces the old "retire") |
| `/view/{token}` | client, no login | read-only rendered proposal + Download PDF button; invalid/expired token → redirect to business homepage. A token that was live until a moment ago (the proposal was just reopened) gets a specific "being updated" message instead — see §6b. |

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

**Salesperson** *(steps 4-6 superseded by §6b - see there for the actual current flow; kept here as the original design intent)*
1. Fill intake form (required fields enforced by the form itself).
2. Optionally attach reference files (from library or new upload → prompted "add to library?").
3. Claude generates the draft; any field that was blank *or* filler text gets a gap marker in that section instead of a fabricated guess.
4. ~~Review the rendered preview. Per section: edit manually, or leave a comment + regenerate (warns first if that section already has a manual edit, since regenerating would overwrite it).~~ **Superseded (§6b):** comments on a section are left by the *approver* during review, not the salesperson while drafting; the salesperson edits/regenerates freely pre-submission and, once changes are requested, resolves the approver's comments (individually or all at once) before regenerating/editing and resubmitting.
5. Pick an approver from the `can_approve` list, submit.
6. ~~If sent back with "changes requested," repeat step 4.~~ **Superseded (§6b):** resubmitting is blocked while any comment from the prior review round is still unresolved.
7. Once approved and sent, see delivery status and — after 7 days — a nudge notification if the client hasn't opened the link yet.

**Approver** *(steps 2-4 superseded by §6b - see there for the actual current flow; kept here as the original design intent)*
1. Get an email notification with a link back into the app (login required — the email itself carries no proposal data or shortcut access).
2. Review the same rendered preview as the salesperson.
3. ~~Leave section comments if changes are needed → "Request Changes" (routes back to salesperson).~~ **Superseded (§6b):** comments are added one at a time (per-section or whole-document, Google-Docs style) while reviewing, not typed into a single fixed form alongside the Request Changes click itself; Request Changes is only enabled once at least one unresolved comment exists.
4. ~~Or **Approve** — but only if no section still has an unresolved gap marker; approving is blocked until every gap is resolved.~~ **Superseded (§6b):** Approve is also blocked while any unresolved comment exists (not just gap markers) - the two gates are mutually exclusive, matching Approve/Request-Changes always being the mirror image of each other.
5. On approve: snapshot frozen, client token generated, client email sent.

**Admin**
1. Invites users by email and assigns `can_create`/`can_approve` up front (§6a) — no password is set by the admin; deactivates users the same way as before. `can_create` now defaults to checked on the invite form and is granted to every existing user (§6b) - everyone can create a proposal by default, not just designated salespeople.
2. Manages the reference library (tag, retire stale files).
3. Views all proposals with filters — status, client, salesperson, approver, date range; can open and edit anything, same as the creator would (§6b).

**Client**
1. Receives an email with a `/view/{token}` link (no login).
2. Sees the proposal rendered read-only, with a Download PDF button that exports the exact same view.
3. Cannot navigate anywhere else in the app — any bad/expired token redirects to the business homepage, never to an internal page or error revealing the app's existence. **Narrow exception (§6b):** a token that was live until a moment ago (the proposal was just reopened) gets a specific "being updated, a new link is coming" message instead of the silent redirect - a genuinely unrecognized token is unaffected.
4. Their view is logged (timestamp, IP) — this is what triggers the salesperson's 7-day nudge if it never happens.

### 6a. Invite-Based User Creation and Forgot-Password

**Status: implemented post-step-16.** The original admin flow had the admin set a new user's password directly. Changed to a GitHub-style invite instead, per direct request: the admin only supplies an email and permissions; the user gets emailed a link to set their own password. A matching forgot-password flow was added at the same time, since one implies the need for the other.

**Two flows, two different secret shapes, deliberately:**
- **Invite**: a long, high-entropy link (`/accept-invite/{token}`), valid 7 days. Nothing to guess, so no separate rate-limiting is needed - the link's own entropy is the security.
- **Forgot password**: a short 6-digit code, valid 15 minutes, sent alongside a link to the reset-password page (not straight to a pre-filled action) - explicitly requested as "a code," not another link to click. A 6-digit code doesn't carry enough entropy on its own, so it's backed by a 5-attempt lockout and short expiry instead.

Both share one `account_tokens` table (`purpose`: `invite` or `reset`) - same shape (a hashed secret, an expiry, a used-at, an attempt counter), just issued/validated differently per purpose. Only a hash of the secret is ever stored, same principle as password storage.

**Forgot-password is for someone who already has a password and forgot it - not a substitute for a lost invite.** A user with no password yet (`password_hash IS NULL`) can't use forgot-password; only an admin can resend their invite. Two different account states, two different recovery paths, not one flow trying to cover both.

**No self-serve account creation.** Both flows require an admin (or, for reset, an existing account) to already exist - there's still no public "sign up" page, matching this app's original invite-only user model (§10's role-based access decisions).

**Addendum (§6b): the person completing an invite now sets their own name, not just their password.** `accept_invite.html`'s form gained a "Full Name" field, pre-filled with whatever the admin typed when creating the account but editable - `User.name` is updated alongside `password_hash` on completion. Small, independent change, but it landed alongside the redesign below since it touched the same invite-completion code path.

---

### 6b. Proposal Flow Redesign — Google-Docs-Style Editing & the Unified Workspace Page

**Status: implemented and verified for real, post-step-16 (see `PROGRESS.md`'s dated "Proposal flow redesign" entries for the full verification detail - real DB, real Claude calls, real HTTP, real browser screenshots throughout).** §6's original persona flows described the proposal's edit/approval cycle as it was originally built: a fixed one-comment-per-section form bundled into a single "Request Changes" submission, three separate pages for creator overview / creator editing / approver review, and a reopen action that simply discarded the old client link. Direct feedback (paraphrased): the real editing experience should feel like Google Docs - inline comments you add as you're looking at a section, async regeneration that doesn't lock you out of the rest of the document, and a full-document regenerate that's clearly "busy" for everyone looking at it, not just the person who clicked the button. This section documents what actually shipped; §6 above is left as the original design record with forward-pointers into here.

**One unified workspace page, not three.** `proposal_detail.html` (creator overview), `proposal_edit.html` (creator's section editor), and `proposal_approve.html` (a separate approver-only review page) are gone. `GET /proposals/{id}` now renders one page whose controls are driven by two independent booleans computed per viewer - `can_edit` (the creator, or an admin) and `can_review` (the assigned approver, or an admin). These are **not mutually exclusive**: a self-approving user, or an admin, can have both true at once, and simply sees both control sets on the same page rather than the app picking a single "mode." The old `/proposals/{id}/edit` and `/proposals/{id}/approve` URLs still exist, but purely as 303 redirects to `/proposals/{id}` - kept because real emails sent before this redesign (approver notifications, changes-requested notices) link to them.

**Comments are Google-Docs style: added one at a time, inline, while reviewing - not a bundled fixed form.** `ApprovalComment.section_key` is nullable (null = a whole-document comment, not tied to one section). A small comment-icon toggle on each section (and one for the whole document) reveals a text box when clicked, otherwise stays out of the way. Each comment is its own `POST /proposals/{id}/comments` call; comments resolve individually (`POST .../comments/{id}/resolve`) or all at once (`POST .../comments/resolve-all`). The gating logic is the mirror image it was always meant to be: **Approve** is blocked while *any* unresolved comment exists (in addition to the pre-existing gap-marker gate); **Request Changes** is blocked while *zero* unresolved comments exist (nothing actionable to send back); resubmitting for approval (after changes were requested) is blocked while any comment from the prior review round remains unresolved - only once every comment is resolved can the salesperson ask for approval again.

**Async per-section editing actually works now - HTMX was wired up but never actually loaded in a browser.** §2a's retrofit added the `hx-post`/`hx-target`/`hx-swap` attributes and the server-side `HX-Request` branch, but the `htmx.org` script itself was never included on any page - meaning every "async" edit/regenerate/comment action had, in every real browser, silently been falling back to the plain full-page-reload path this whole time (by design safe, since the fallback exists for JS-disabled browsers - but not the intended behavior). Caught only once this redesign was driven with an actual Playwright browser session instead of just `TestClient`. Fixed by self-hosting a pinned `htmx.org` build (`app/static/htmx.min.js`, matching this app's no-external-CDN convention for its own JS/CSS) and loading it on the workspace page. Confirmed for real: editing a section's content now fires a genuine XHR with zero page navigation, and the rest of the page stays interactive while it happens.

**Full-document regenerate has a real shared lock, not just a disabled button in one browser tab.** A collapsible "Raw Inputs" tab (creator/admin only - the approver never sees it) makes the eight intake fields editable and the reference-file list manageable, plus a "Regenerate Full Proposal" action. Triggering it (after a warn-and-confirm step if any section has a manual edit that would be overwritten, same two-step pattern as per-section regenerate) sets `Proposal.is_regenerating = True`, commits, and schedules a FastAPI background task to do the several sequential Claude calls after the response has already redirected back. While that flag is true, **every** viewer's `GET /proposals/{id}` - the triggering user's included, and a completely different approver's session, not just "this browser tab is busy" - renders a small self-polling "this proposal is being updated, please wait" page instead of the real content, and every mutating route (edit, regenerate, comment, submit, approve, request-changes, reopen, reference changes) rejects with a 409 if attempted while the lock is held. Verified against a real live server with two independent HTTP sessions, not just `TestClient` (whose background tasks run synchronously and can't actually demonstrate this).

**Reopening a sent/approved proposal now leaves a trace of the old link, instead of just discarding it.** The outgoing `client_token` is copied into `Proposal.retired_client_token` (only the single most recent one is kept) before being cleared. This backs one narrow, deliberate exception to §7's "a bad link reveals nothing" rule: `/view/{token}` for a token that no longer matches `client_token` but does match `retired_client_token` shows "this proposal is being updated - a new link will be sent to you shortly" instead of the generic silent homepage redirect. A genuinely unrecognized token - one that was never valid, or was retired more than one reopen ago - gets the exact same silent redirect as always; the exception only ever covers a link that really was live a moment ago. The reopen action itself now requires an explicit confirmation naming the consequence (the client's current link will be invalidated) before it fires.

**The proposals list (`/proposals`) is role-scoped with filter tabs, not one flat mixed list.** Columns: client/company, creator, created date, last modified, approver (blank if none), status. An approver's list carries three tabs - **All**, **Created**, **Awaiting My Approval** - narrowing the same underlying `created_by = me OR approver_id = me` scope; a pure salesperson (no `can_approve`) never sees the tabs at all, since none of them would exclude anything they can already see. Seeing a colleague's proposal you're neither the creator nor assigned approver on still has no dedicated feature - it's the same `/view/{token}`-style copy-link mechanism as the client link, just shared internally; a **Copy Client Link** button on the workspace page (visible whenever the proposal has a live `client_token`) makes grabbing that link a one-click action instead of digging through the database.

**`can_create` stays a real, admin-editable flag - it just defaults to granted for everyone now.** Originally a deliberately-assigned capability (§10); changed so every new invite defaults to checked and every existing active user was granted it in a one-time data migration, per direct instruction that "everyone can create a new proposal." An admin can still revoke it from a specific user - the flag didn't go away, only its default.

---

### 6c. Reference Files: Attach-During-Creation + Library Trash

**Status: implemented post-§6b (2026-09-08).** Two more direct requests, planned and implemented together since both are about reference files.

**Creating a proposal and attaching reference files are no longer two disconnected steps.** `/proposals/new` used to take only the 8 intake fields; attaching a reference file required a *separate* trip to the workspace page's Raw Inputs tab after the proposal already existed, followed by a *third*, separate "Generate Proposal" click. Now all of it happens on the one intake page: attach files from the shared library or upload from your own device (each upload optionally also joining the shared library, prompting for a name and short description at that point) - up to 5 per proposal, blocked with a clear message on a 6th attempt - and one "Create Proposal" click creates the proposal, attaches everything selected, calls Claude, and lands straight on the finished workspace page. Since every intake field is `NOT NULL`, there's no proposal row to attach *to* until that final click - reference files are tracked independently (a library pick just rides along as a hidden id; a device upload creates a real `ReferenceFile` row immediately) until the proposal itself is created.

**The reference library gained a real Trash, replacing the old "Retire."** Retire only ever flipped `is_library` to hide a file from future proposals - no real removal, no ownership check, one file at a time, no name/description on upload. Now: upload takes several files in one submission, each with its own name and description; "Move to Trash" (owner or admin only) is a real, recoverable delete with a 30-day grace period before an item auto-purges - checked lazily on any library page load, following the same no-background-jobs precedent as the existing 7-day nudge (§9 - Render's free tier has no cron), not a real scheduled job. Trashing is blocked, naming the specific proposal(s), if the file is still attached to a `draft`/`pending_approval`/`changes_requested` proposal - an already-`approved`/`sent` proposal's reference list lives in its frozen snapshot, so it's never a blocker. Preview (open the real file inline in a new tab) and multi-select bulk trash round out the library page.

### 6d. Terminology Rename, Reference-File UX Overhaul, Self-Approve, Send Confirmation, and a Full UI Consistency Pass

**Status: implemented post-§6c (2026-09-09).** A longer batch of direct feedback and follow-up requests, all converging on the same underlying pages (intake form, reference library, workspace) plus one cross-cutting business-workflow addition. Full blow-by-blow detail, including every real bug found and how it was verified, lives in `PROGRESS.md`'s dated entries covering this same window - this section captures the durable decisions, not the process.

**"Client Name" / "Company Name" renamed to "Representative" / "Client/Company" everywhere user-facing.** The original two-field split (an individual's name and their employer, stored separately) was correct data-wise but read ambiguously - "client" sounding like it should mean the account, not the person. Relabeled across the New Proposal form, the workspace, every admin/dashboard list, and the approver-notification email, with **Client/Company always shown first, Representative second** (matching how a proposal itself is naturally framed - who it's for, then who you're talking to there). Deliberately a *label-only* rename: the underlying `client_name`/`client_email`/`company_name` database columns are untouched, and the client-facing document/delivery email (which never showed a field label, just the name in a sentence) needed no change at all. A full rename to the database layer was considered and explicitly rejected as unnecessary migration risk for a purely cosmetic ambiguity fix.

**Generated proposals no longer repeat the client's name, company name, and "Koya Talent" in every section.** Compared against the reference `proposal-template.md`, which names each of those only 3-4 times across an entire document (title, one Introduction mention, sign-off), against real generated output that was doing so in nearly every one of the 6 sections. Root cause: the system prompt told Claude how to distinguish the two names but never told it to be sparing about using either. Fixed with an explicit restraint instruction (name the company once, meaningfully, in the Introduction - the document's own title and wordmark already establish who it's for and who wrote it before any of Claude's own prose is read - then write every later section directly, without re-announcing identity). Verified with a real generation call: down to 1 company mention, 1 "Koya Talent" mention, 2 client-name mentions (down from repeating in nearly every section).

**Reference-file upload redesigned around a Google-Forms-style pattern, with real per-file control.** Both the Reference Library page and the New Proposal page's device-upload panel now use a drag-and-drop dropzone (replacing the raw native file input) that accumulates files across repeated picks instead of replacing the selection, with independent Name/Description/Tags per file and, on the New Proposal page, an independent per-file "add to library" choice - previously one shared description and one shared library flag applied to an entire batch. Unsupported file types are now rejected client-side, immediately, next to the picker (mirroring the backend's own allowlist) rather than only failing after a full round trip whose error banner was easy to scroll past.

**A file uploaded but never added to the library, and later detached from every proposal, is now cleaned up automatically instead of becoming a permanent orphan.** Previously such a file's `ReferenceFile` row and Supabase Storage object were deliberately left behind on removal (by design, to avoid touching a row another proposal might still cite) with no path in the UI to ever reach it again. A lazy 7-day purge (same no-background-jobs pattern as the trash auto-purge above) now removes a reference file once it's `is_library=false`, unattached to *every* proposal, and older than 7 days - never touching a file still attached to any proposal regardless of status, and never touching a library file regardless of usage. Paired with a real warning at the one point it's still actionable: removing a non-library reference from a proposal now shows a confirm dialog naming the file and stating it will be permanently deleted in 7 days unless re-attached or added to the library - not a silent removal followed by an unrecoverable loss days later.

**Self-Approve**: an admin or a `can_approve` user can now approve a proposal they created (or otherwise have edit access to) in one click, without first submitting it to themselves and then switching hats to approve it - shown as a second button beside "Submit for Approval," never replacing the normal submit-to-a-different-approver flow. Goes through the exact same snapshot-freeze/token-issue path as a normal approval, so a self-approved proposal is indistinguishable from a normally-approved one except for who's recorded as `approver_id`.

**Send-to-Client now requires confirming (or fixing) the representative's name and email first.** The representative's email was previously only ever visible by opening the collapsed Raw Inputs panel, which disappears entirely once a proposal is finalized - exactly when a wrong delivery address would matter most. A `.client-details-line` now shows Client/Company, Representative, and Representative Email in every status, and clicking "Send to Client" opens a dialog with the same three fields (Company read-only, Representative name/email editable) that must be confirmed before the send actually happens - correcting a typo here doesn't require the heavier Reopen-for-Editing flow, since it has nothing to do with the approved proposal content itself.

**A UI consistency pass across the whole app**, prompted by "does this look client-ready": count badges reserved for genuinely actionable counts (pending approvals, unopened nudges) rather than used as a generic "alert" style for plain totals; destructive actions (trash, permanent delete) styled consistently in red with an icon, distinct from ordinary secondary actions; disabled buttons restyled to read as clearly inert rather than a barely-dimmed version of the enabled state; a real CSS layout bug fixed in the admin filter grid and the invite-user permissions checkboxes (both confirmed via computed-layout inspection, not just visual guesswork); and the same primary-button treatment applied consistently to the same logical action (e.g. "New Proposal") wherever it appears in the app.

---

### 6e. Security Audit: Rate-Limiting Added, CSRF Deliberately Skipped

**Status: implemented post-§6d (2026-09-09).** A production-readiness audit (the Koya program's graded checklist - error handling, edge cases, cost/resource awareness, idempotency, security - adapted to this FastAPI codebase since no n8n workflow exists here) found two security gaps. Both were investigated before deciding what to build; full process detail lives in `PROGRESS.md`'s dated entry for this window.

**Rate-limiting on `/login` and `/forgot-password`: added.** Neither route had any throttling before this - unlimited automated password guessing against any known email, or unlimited reset-code spam, with only bcrypt's own hashing cost as friction. `slowapi` (`app/rate_limit.py`), `5/minute` per IP (`get_remote_address`, in-memory storage - no Redis, matching this app's existing no-extra-infrastructure precedent from the trash/orphan purges and the live dashboard nudge query), with a custom 429 page matching the app's existing simple-card style rather than the library's raw JSON default. Keyed on IP alone, not IP+email, since a combined key would need reading form data before slowapi's own request handling - not the library's documented pattern - and per-IP already closes the actual gap (there was no mitigation at all before this).

**CSRF token protection: investigated, deliberately not built.** This app already sets `SameSite=Lax` on its session cookie (§2/§10), which defeats the classic forged-cross-site-form CSRF attack on its own - a browser withholds the session cookie on a cross-site POST under `Lax`, so a forged request arrives unauthenticated and is rejected the same as any anonymous one. The residual gap (pre-2018 browsers with no `SameSite` support) isn't a real threat for an internal B2B tool. Both CSRF libraries considered had real drawbacks that would have outweighed that residual gain: `starlette-csrf` validates its token only via an HTTP header, never a form field, incompatible with this app's 38 plain-HTML form submissions without rewriting all of them through JavaScript; `fastapi-csrf-protect` supports form-field tokens but only via per-route `Depends()`, not global middleware, so a future new route could ship unprotected with nothing structurally catching it. Decided with the user to skip full CSRF protection entirely, including a lighter Origin/Referer-header-check alternative offered as a fallback - not a deferred TODO, a closed call, revisit only if the threat model changes (e.g. a public-facing portal gains real state-changing actions beyond the current read-only `/view/{token}`).

---

## 7. Edge Cases and How They're Solved

| Edge case | Solution |
|---|---|
| Required field left blank | Form validation blocks submission |
| Field filled with filler/placeholder text | Claude (not form validation) judges insufficiency at generation time and inserts a gap marker instead of fabricating |
| Approver tries to approve with unresolved gaps | Approve action is disabled/blocked until all gap markers are resolved |
| Regenerate clicked on a manually-edited section | Warn first ("this section has manual edits — overwrite?") before regenerating |
| Proposal edited after approval | Requires an explicit, confirmed "reopen" action; reopening invalidates the old client token and issues a new one on re-approval, so no live client link ever points at stale content. **Since §6b:** the old token is kept as `retired_client_token` so a client who still has that exact link is told "this is being updated," not left with silence |
| Approver's `can_approve` revoked mid-flight | Admin can reassign the pending proposal to a different approver |
| Approver clicks "Request Changes" with no unresolved comment on the proposal | Blocked — since §6b, comments are added individually while reviewing (not typed into the Request Changes form itself); the action stays disabled until at least one unresolved comment exists |
| Approver tries to Approve while any comment is still unresolved | **New since §6b** — blocked alongside the existing gap-marker gate; Approve and Request Changes are always each other's mirror image (one enabled only when the other is disabled) |
| Salesperson tries to resubmit for approval while a comment from the prior review round is still unresolved | **New since §6b** — blocked with a clear message; every comment must be resolved before asking for approval again |
| Approve / Request Changes attempted on a proposal that isn't `pending_approval` (already approved, still a draft, etc.) | Blocked with a clear error — both actions are only valid from that one status, enforced server-side, not just hidden in the UI |
| A `can_approve` user who isn't the proposal's assigned approver opens the proposal's workspace page | 403 — access is scoped to `can_edit` (creator or admin) or `can_review` (assigned approver or admin); having neither relationship to that specific proposal is a 403 regardless of general capability |
| A colleague tries to write to a proposal while its full-document regenerate is in progress | **New since §6b** — every mutating route (edit, regenerate, comment, submit, approve, request-changes, reopen, reference changes) returns 409 while `Proposal.is_regenerating` is true; every viewer's page shows a "please wait" placeholder instead of stale or half-updated content in the meantime |
| Claude API fails/times out | Intake data is saved *before* calling Claude, so a failed generation never loses the salesperson's input — just retry |
| Claude returns malformed output | Validate response shape before writing to `sections`; on mismatch, surface "generation failed, retry" rather than saving garbage |
| Regenerate spam (cost control) | Soft cap on regenerations per section |
| Prompt injection via client input or uploaded files | System prompt treats all intake/reference content strictly as data to summarize, never as instructions |
| Reference file in an unsupported format uploaded (e.g. Word/Excel/PowerPoint) | Rejected at upload with a message asking the user to convert it to PDF first — not silently accepted, degraded, or force-parsed |
| Reference file fails to download from storage at generation/regeneration time | That one file is flagged unavailable in the prompt sent to Claude; generation still proceeds for every other section/reference rather than failing the whole call |
| Reference library file retired while already attached to a proposal | Retiring only removes it from the list offered for *new* attachments — an existing proposal's attachment (and the "References Used" display) is untouched |
| A device-uploaded reference file is never added to the library and gets detached from every proposal (removed, or the New Proposal form was abandoned before Create) | **New since §6d** — a lazy 7-day purge deletes it (never touches a file still attached to any proposal, or any library file); removing a non-library reference shows a confirm warning naming the file and the 7-day deadline before that removal is even confirmed |
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

### 8a. Cost Optimization: Caching (yes) vs. Summarization (no)

**Status: implemented in step 8** (see `PROGRESS.md`) — this section was written as a decision to build against before reference files existed; both the caching mechanism and the file-handling approach below shipped together once there was something to cache.

Once reference files (§14 step 8) are attached to a proposal, every generate/regenerate call resends the system prompt, intake data, and any attached reference documents in full. Two techniques were considered to reduce that repeated cost — one adopted, one deliberately rejected, both driven by the same principle: **accuracy takes priority over cost savings**, since the cost involved is already trivial at this volume (§8 above).

**Adopted — prompt caching, 5-minute (standard) TTL, not the 1-hour extended tier.**
- Anthropic's prompt caching is content-addressed: a cache breakpoint after the static prefix (system prompt + intake + reference files) means repeat calls sharing that exact prefix pay a much lower rate to reread it, while any edit to that content is simply a cache miss — never stale or wrong content served, only a fresh full-price call. This makes caching a pure cost optimization with **zero accuracy tradeoff**, unlike summarization below.
- The realistic usage pattern — a salesperson reviewing one proposal and regenerating a few sections back-to-back — happens in a burst of a few minutes, and the standard cache's 5-minute TTL **refreshes on every hit**, so an active session stays warm throughout without needing the longer tier.
- The 1-hour extended cache costs roughly 2x the normal input rate to write (vs. ~1.25x for the 5-minute tier) to guard mainly against long idle gaps between edits — not the case being optimized for here, so the extra cost and complexity wasn't justified.

**Considered and rejected — pre-summarizing reference files (e.g., at upload time, via a cheaper model) and sending the summary instead of the full document on every call.**
- This would have helped a different scenario than caching does: cost amortization for a *library* file reused across many separate proposals over time (days/weeks apart), which a 5-minute cache never touches.
- Rejected because it's lossy by nature, and lossy directly conflicts with §10's "cited directly in text when relevant" requirement — a summary can smooth over or drop the exact figure, date, or quote a proposal needs to cite precisely from the source.
- Given §8's cost analysis already treats the whole proposal's AI spend as a rounding error, and caching already covers the cost case that matters most (same-session regeneration) with no fidelity cost, paying a real accuracy cost to guard against an already-small and mostly-solved cost problem wasn't a good trade. If a specific case later justifies it (e.g., someone attaches a genuinely huge document), it can be handled narrowly then, not built in now as a default.
- Reference files, once built, are always sent at full fidelity — no summarization step, no "extracted summary" column in `reference_files`.

### 8b. Reference File Formats: Native Content Only, No Office Formats

**Status: implemented in step 8.** This spec never named a file format for reference files (§3, §14 step 8); the actual scope was decided during the build and is recorded here so it isn't mistaken for an oversight.

**Supported: `.txt`, `.md`, `.pdf`, and images (`.jpg`/`.jpeg`/`.png`/`.gif`/`.webp`).** Every one of these is sent to Claude as a native Messages API content block — a `document` block for PDF (the actual bytes, so Claude reads real pages, tables, and layout itself) and plain text, an `image` block for images — never through a local parsing/extraction library. This directly implements the "always sent at full fidelity" principle from §8a: a third-party parser (e.g. a PDF text-extraction library) is text-only and layout-blind, and would silently drop a scanned page, a chart rendered as an image, or a table it can't reconstruct in reading order, without ever erroring — exactly the kind of silent data loss full fidelity is meant to rule out.

**Explicitly not supported: `.docx`, `.xlsx`, `.pptx` (or their legacy `.doc`/`.xls`/`.ppt` equivalents).** These are rejected at upload with a message asking the user to convert the file to PDF first, rather than a generic "unsupported file type" error — deliberately, not because it was out of time to build:
- Claude does not read Office Open XML formats natively the way it reads a PDF. The mechanism behind products like Claude.ai accepting a `.docx` upload is Anthropic's **Skills** feature (an Anthropic-managed skill per format, e.g. `skill_id: "docx"`) running inside a sandboxed **code-execution container** — Claude executes real parsing code against the file, it doesn't "read the bytes" the way it does a PDF.
- That mechanism needs three things this app doesn't have and wasn't worth adding for this scope: (1) uploading the file via Anthropic's separate Files API first, not just a request content block; (2) the `code_execution` tool plus a `container.skills` entry for the format; (3) most importantly, skills run via an autonomous tool call, which cannot happen in the same call as this app's forced `tool_choice` (the hard rule behind every generate/regenerate call, per `CLAUDE.md` — structured output is always forced tool-use, never free-form parsing; see §8e for why that rule exists in the first place). Supporting Office formats this way would mean a **separate preliminary call per file** (auto tool-choice, code execution enabled) before the existing structured-output call — a second Anthropic API surface, added latency (container start-up), and a new billing dimension (container runtime) this build's cost analysis (§8) never covered.
- Two lighter alternatives were also considered and rejected: extracting text/images locally via pure-Python libraries (`python-docx`/`python-pptx`/`openpyxl`) — layout/positioning wouldn't be preserved, a smaller version of the same fidelity concern that ruled out `pypdf` for PDFs; and converting to PDF server-side via LibreOffice headless — pixel-perfect, but requires a system-level binary that isn't pip-installable, changing the deployment target from a stock Python buildpack to a custom Docker image (a §9/step 16 concern, not a library choice).
- Given all three routes carry a real cost (new API surface, fidelity loss, or infra change) for a format this app can simply ask the user to convert instead, the decision was to not support Office formats at all rather than pick the least-bad option.

### 8c. Email Provider: Mailjet (after Brevo, after Resend/SendGrid)

**Status: implemented post-step-14** (see `PROGRESS.md`'s post-step-14 follow-up). This spec originally named Resend/SendGrid only as examples (§2); Resend was the provider actually built and verified first (steps 9/12), then replaced by Brevo once the real requirement — send to arbitrary real recipients, on no budget (§8d) — collided with a hard limitation Resend's free tier has and Brevo's doesn't.

**Why the switch, not just "Brevo is better":** Resend's real-send integration was verified working in step 9, but step 12 hit its actual limit — Resend's free tier hard-blocks sending to anyone but the account's own verified email until a domain is DNS-verified (SPF/DKIM/DMARC), so a proposal addressed to a real client got a `403` every time. That's not a quota problem to ration around — it's a functional block on the app's core job (emailing actual clients) that only lifts once a domain is verified, which costs nothing in dollars but does cost DNS access and setup time this project didn't want to gate on.

**What was confirmed about each option before deciding (not assumed):**

| | Free tier | Real-recipient sending, pre-domain-verification | Multi-recipient (to/cc/replyTo) in one call |
|---|---|---|---|
| **Resend** | 3,000/mo, capped 100/day, permanent, no card | **Blocked** — 403 to anyone but the account owner until a domain is DNS-verified ([account quotas/limits](https://resend.com/docs/knowledge-base/account-quotas-and-limits)) | Supported, but blocked by the restriction above in practice |
| **SendGrid** | **None for new signups** — permanent free tier retired May 27, 2025; new accounts get a 60-day trial (100/day), then plans start at $19.95/mo ([SendGrid free-plan status](https://costbench.com/software/email-api/sendgrid/free-plan/)) | N/A — not free past 60 days | Supported |
| **Brevo** | 300/day (~9,000/mo), permanent, no card ([Brevo email API](https://www.brevo.com/features/email-api/)) | **Allowed** — verifying one sender address (a 6-digit code, no DNS) is enough to send to any recipient; full domain authentication is recommended for deliverability but isn't a functional gate ([domain authentication FAQ](https://help.brevo.com/hc/en-us/articles/17286219877778-FAQs-About-domain-authentication-Brevo-code-DKIM-DMARC)) | Supported natively — `SendSmtpEmail` has first-class `to`/`cc`/`bcc`/`replyTo` fields ([send a transactional email](https://developers.brevo.com/docs/send-a-transactional-email)) |

**Decision: Brevo**, on two points that directly gate this app's requirements, not general provider preference:
1. **It's the only one of the three where a real client can actually receive an email on a $0 budget without a DNS/domain step first** — SendGrid's free tier no longer exists in a form that reaches past 60 days, and Resend's free tier exists but functionally can't email a real client until a domain is verified, which is exactly the wall step 12 hit.
2. **Its `to`/`cc`/`replyTo` fields are all first-class in one API call**, which is what the central-inbox/CC/Reply-To architecture (§8d) needed — no separate call or workaround required to CC a salesperson and redirect replies at the same time.

Domain authentication is still worth doing on Brevo eventually for deliverability (fewer spam-folder landings, especially at Gmail/Yahoo/Microsoft) — but unlike Resend, it's an optimization to do later, not a blocker to get real sending working today.

**Backup path added post-step-16: Gmail SMTP.** `send_email()` tries Brevo first, and only falls back to a personal Gmail account (SMTP, app password) if Brevo is unconfigured or a real send attempt to it fails (e.g. the account gets suspended) — only raising an error if both fail. This is a backup, not an equal alternative: Gmail's SMTP relay can only send *as* the authenticated Gmail address itself (no domain delegation), so a fallback send shows that personal address as the sender rather than the central `EMAIL_FROM_ADDRESS`. `cc`/`reply_to` still work identically either way.

**Manual override, originally `USE_BREVO=false`, now `USE_MAILJET=false`.** Skips the primary provider entirely — not even checking whether its credentials are present — and always uses Gmail, without needing to remove real credentials from `.env` to force that (e.g. during a known provider outage). Defaults to `true`, matching the original try-primary-first behavior when unset.

**Post-deployment: Brevo's account was suspended, replaced with Mailjet.** Once real invite/notification emails were flowing in production, the Brevo account got suspended — with no email provider working at all, since the Gmail SMTP backup path (added specifically for exactly this scenario) turned out to never have been a real backup on Render: Render's free web-service tier blocks outbound SMTP at the network level (confirmed by the actual production error, `[Errno 101] Network is unreachable`, when Gmail was attempted), so that fallback was silently dead the entire time this app has been deployed. **Mailjet was chosen as the replacement** after confirming, directly against Mailjet's own docs (not assumed): a free tier of 6,000/month (200/day), no card required; the same single-verified-sender model that made Brevo work (full DNS/domain verification is a deliverability recommendation, not a functional gate on sending to real recipients); and an HTTPS JSON API (`api.mailjet.com`), which — unlike SMTP — Render does not block. `Cc` is a first-class field on Mailjet's `Send API v3.1`; `reply_to` is set via its documented `Headers` mechanism (`{"Reply-To": ...}`) instead of a dedicated top-level field, since v3.1 doesn't have one. The switch is a straight swap in `app/services/email.py` — same shape (verified sender, HTTPS POST, JSON body, Gmail fallback if it fails) — `USE_BREVO`/`BREVO_API_KEY` became `USE_MAILJET`/`MAILJET_API_KEY`/`MAILJET_API_SECRET` (Mailjet authenticates with an API key *and* secret key via HTTP Basic Auth, not the single bearer key Brevo used). The Gmail SMTP fallback itself was left in place as-is (still genuinely useful locally, or on any host that doesn't block outbound SMTP) rather than replaced with a second HTTPS-based provider — a real known-broken-on-Render backup path stayed out of scope for this specific fix.

### 8d. Email Architecture: Central Inbox, CC/Reply-To, and Why Each of the Three Flows Exists

**Status: implemented post-step-14.** This spec's original persona flows (§6) named *that* an approver gets emailed and a client gets emailed, but not the sending architecture behind it. The architecture below — and the third flow, changes-requested notifications, which didn't exist before this — was added once the gap became concrete: every proposal-related email now sends from one central address (`EMAIL_FROM_ADDRESS`), never a per-salesperson address, with `cc`/`reply_to` set per-call depending on who the email is about.

**Why a central address instead of sending "as" each salesperson:** the original assumption was that each salesperson's own email would appear as the sender. In practice, a transactional email API can't actually send *from* an individual employee's personal or Google Workspace inbox without that person's mailbox being individually configured as a verified sender/domain owner in the provider — a real setup burden per hire, and a fragile one (an employee leaving means rotating that config). One verified central sender solves this once, for every salesperson, forever — and every recipient still sees exactly whose proposal it is because the subject line, body, and (for client delivery) CC name the specific salesperson.

**The three flows, and why each one is shaped the way it is:**

1. **Approver notification** (salesperson submits → central inbox emails the assigned approver). Plain send from the central address, no CC/reply-to override — the approver's job is to act inside the app (review, approve, or request changes), not to reply to the email itself, so there's no inbox the reply needs to reach beyond the approver's own.
2. **Client delivery** (approver approves → central inbox emails the client, **CC's the salesperson who created the proposal**, **Reply-To set to that salesperson**). The client needs to hear from a legitimate, deliverable business address, not an individual's; the salesperson is CC'd so they see exactly what the client received, when; and replies route to the salesperson specifically — not the shared inbox — because they're the one with the actual client relationship and the context to answer a follow-up. Nobody else at Koya Talent should be the one fielding "can we adjust the timeline?" from a client they've never spoken to.
3. **Changes-requested notification** (approver requests changes → central inbox emails the proposal's creator with the specific section comments and a link back to the edit page). This flow didn't exist before this change — a salesperson previously found out only by checking the dashboard, with no push notice at all. It closes that gap the same way approval already worked: the person who needs to act (here, the salesperson revising the proposal) gets told the moment there's something to act on, not left to notice it themselves.

**Why `cc`/`reply_to` are per-call parameters, not global settings:** unlike `EMAIL_FROM_ADDRESS`/`EMAIL_FROM_NAME`, which really are fixed, app-wide config, who gets CC'd and who a reply should reach depends on *which proposal* the email is about — specifically, its creator. Baking that into global settings would make every email CC the same person regardless of who actually owns the proposal; it has to be resolved per-send from the proposal's own data.

### 8e. Forced Tool-Use: Why Every Claude Call Returns a Tool Call, Never Prose

**Status: implemented in step 6, standing rule since.** Every call to Claude in this app (`app/services/proposal_generation.py`) sets `tool_choice={"type": "tool", "name": "<tool_name>"}` — Claude is *forced* to respond by calling a specific tool with a specific input schema, never free to answer in prose. This was a build-time decision, not something this spec originally specified, and it was never given its own write-up here — it's referenced only in passing at §8b as a constraint on Office-file support. Documented properly here since "why a tool call was used" should be checkable in one place, not just implied by a passing mention.

**Why, concretely:**
- **The alternative — asking Claude to "reply with JSON" in prose and parsing the response — is measurably more fragile.** Free-form JSON-in-prose can be preceded/followed by commentary, wrapped in markdown code fences inconsistently, or subtly malformed in ways a regex/strip-based parser has to guess around. A forced tool call instead returns an already-structured `tool_use.input` dict directly from the API - there's no text to parse at all, because there was never freeform text to begin with.
- **This app's own requirement makes the difference concrete, not theoretical.** Spec §7 requires validating response shape before writing anything to the DB, and surfacing "generation failed, retry" on a mismatch — rather than saving a guess. Forced tool-use plus a `input_schema` (required keys, types) is what makes "the shape is either right or it's a `GenerationError`" enforceable at all; parsed-from-prose JSON has no equivalent schema contract to validate against before that parsing even happens.
- **It composes with a second validation layer, not instead of one.** The tool's `input_schema` is Claude's-side contract; every response is *also* validated against a Pydantic model (`GeneratedSections`/`SectionOutput`) before touching the DB - two independent checks, not "trust the tool schema and skip validating." See `_call_claude_validated()`.

**What this ruled out, and why it stayed ruled out:** Anthropic's Skills feature (the mechanism behind Claude.ai reading `.docx`/`.xlsx` uploads) runs via an *autonomous* tool call inside a code-execution container - fundamentally incompatible with a single call that's already forced onto one specific structured-output tool. This is the actual, concrete reason Office reference-file formats aren't supported natively (§8b) - not an oversight, a direct consequence of this decision.

**The real-world cost of getting this partially wrong, and how it was actually caught (step 7):** even with forced tool-use, Claude's *content* toward the model's own tool call can still misbehave - real testing found Claude leaking a stray `</content>` closing tag plus a leaked `has_gap` marker into the `content` string on single-section regenerate calls, well above baseline (see `PROGRESS.md` step 7 for the exact reproduction). Critically, this failure was **not independent random noise**: one specific section ("Deliverables," naturally written as a bulleted list) reproduced the identical malformed output across six separate calls, because a failed regenerate never overwrites the stored draft, so every retry re-fed Claude the same problematic input. A blind "just retry" approach - which is all forced tool-use alone would have bought - doesn't fix a failure that's deterministically tied to specific content. The actual fix was a targeted repair function (`_repair_leaked_output`) that recognizes the specific leak pattern via regex, extracts the real payload, and re-validates it before giving up - "validate before writing" from §7, made tolerant enough to recover a near-miss instead of discarding it. The lesson generalized into a standing rule (`CLAUDE.md`): a new reproducible malformed-output pattern gets a targeted repair function, not just more retries.

---

## 9. Deployment Decisions

- **Hosting: Decided: Render**, over Railway, on a $0-budget basis — Railway retired its permanent free tier (a one-time trial credit, then a paid Hobby plan), while Render's free web-service tier is permanent (750 instance-hours/month, one always-on service fits inside that), includes a free custom domain with managed TLS, and (like Railway) supports a plain Dockerfile deploy — necessary here since Playwright's headless Chromium (§2/step 11) needs real system libraries (`libnss3`, `libatk-bridge2.0-0`, etc.) that only a Docker-based build, not an auto-detected Python buildpack, can install via `playwright install --with-deps`. Known trade-off of the free tier: the service sleeps after 15 minutes idle, with a 30-50s cold start on the next request — since PDF export's own Playwright timeout is 30s, a cold container could make the *first* PDF request after a period of inactivity fail or run close to that timeout; upgrading to a paid always-on instance later removes this, but wasn't justified for this build's budget.
- **Database**: Supabase Postgres, reusing the same project/instance from Week 2 rather than a separate one — Supabase's free tier caps a personal account at two projects, so a single shared instance is used as a central Postgres host for multiple Koya projects rather than one project per app. To keep this app's tables fully isolated from other projects sharing the instance (schema-level separation, not a separate database), all of this app's tables live in a dedicated `proposal_app` Postgres schema rather than the default `public` schema — see §3.
- **File storage**: **Decided: Supabase Storage** (not a separate S3-compatible service) — reuses the Supabase project already in use for Postgres rather than standing up a new external account, and matches the spec's own lean toward it. Reference files and generated PDFs go there, not the app's local disk, which doesn't survive redeploys on most PaaS platforms.
- **Secrets**: Claude API key, email API key, DB URL — all as environment variables, never committed. `render.yaml` (§14 step 16) declares every required variable's *name* with `sync: false`, so Render prompts for each one's real value in its dashboard rather than storing them in the repo.
- **Domain for client links**: a short, clean subdomain (e.g. `proposals.yourcompany.com`) reads more trustworthy in a client's inbox than a raw platform-generated URL. Render's free tier includes custom-domain support with managed TLS at no extra cost, so once a domain is registered, attaching it is a Render-dashboard step (add the domain, point its DNS at Render per the CNAME/A record Render provides), not an infra decision — registering the domain itself remains on the human (§13).
- **Deployment artifacts (§14 step 16)**: `Dockerfile` (Playwright/Chromium system deps via `--with-deps`, `poetry install`, then `docker-entrypoint.sh`), `docker-entrypoint.sh` (`alembic upgrade head` before starting `uvicorn`, so every deploy self-migrates rather than needing a manual migration step), `.dockerignore`, and `render.yaml` (Render's Blueprint format — one `runtime: docker` web service, `healthCheckPath: /health` using the health route already built in step 3, and every secret declared by name only).
- **Background jobs**: still none (§7/§14's 7-day nudge stays a live dashboard query, per the open-items note carried since step 13) — Render Cron Jobs aren't available on the free tier, and the live-query approach already works without needing one; revisit only if a real scheduled job becomes worth paying for.

---

## 10. Decisions Made — Full Log (explicit and implicit)

**Explicit (you chose directly)**
- Backend: FastAPI
- Document approach: web preview that renders exactly as the PDF, downloadable as that same PDF
- Approval flow: comment-per-section → targeted regenerate → in-app approval → shareable link
- Database: Postgres
- Role model: capability-based (`can_create`/`can_approve`), self-approval allowed. **Since §6b: `can_create` defaults to granted for everyone** (every invite, every existing user) rather than a deliberately-assigned few — it stays a real, admin-revocable flag, just with a different default
- Client access: unguessable UUID token, 30-day expiry, access logging, no email-confirmation gate
- Visibility: creator/approver see their own; admin sees all with filters
- Versioning: section-level history + frozen approval snapshot
- Regenerate-conflict handling: warn before overwriting manual edits
- Reference material: reusable library + per-proposal upload, listed as visible references, cited directly in text when relevant
- Approver notification: email with a link back into the app (login required)
- Intake validation: required fields compulsory on the form; filler text is Claude's job to catch, not the form's
- Approval gate: blocked while unresolved gap markers exist
- Reopen behavior: invalidates old client token, issues a new one on re-approval. **Since §6b:** the old token is kept (as `retired_client_token`) rather than discarded, specifically so a client who still has that exact link can be told it's being updated instead of getting silence — a narrow, deliberate exception to the "bad link reveals nothing" rule, scoped only to a link that really was valid a moment ago
- Full-document regenerate lock (§6b): a real shared lock (`is_regenerating` DB flag + a background task), not just a disabled button in the triggering browser tab — chosen so every viewer, not only whoever clicked, sees the "please wait" state
- Unified workspace page (§6b): consolidate the three separate creator-overview/creator-edit/approver-review pages into one role-aware page, rather than keeping them separate and adding comment/lock features to each independently
- Comment model (§6b): Google-Docs-style inline comments added one at a time while reviewing (optionally on the whole document, not just one section), replacing the original single fixed per-section comment bundled into the Request Changes submission
- Scope boundary: tool's job ends at delivery/download, no negotiation loop
- Feature: 7-day unopened-link nudge notification to the salesperson
- Client link expiry: 30 days
- Database hosting: one shared Supabase instance reused across Koya projects (Supabase's free tier allows only two projects), with each project's tables isolated in its own Postgres schema (`proposal_app` for this app) rather than a separate project per app
- Cost optimization for Claude calls: prompt caching (5-minute standard TTL) adopted; pre-summarizing reference files rejected as a lossy tradeoff not worth making given the accuracy requirement in §10's "cited directly in text" decision and how small the cost already is (§8a)
- Reference file formats: limited to what Claude reads as a native content block (PDF, plain text/Markdown, images) — Office formats (`.docx`/`.xlsx`/`.pptx`) explicitly not supported after weighing Anthropic's Skills/code-execution route (real, but conflicts with this app's forced-tool_choice design and adds an uncosted latency/billing surface) against local extraction (fidelity loss) and server-side PDF conversion (a system-dependency/deployment change) — see §8b

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

- Create accounts and get API keys: Anthropic (Claude API), email provider (Mailjet — see §8c for why, over Resend/SendGrid/Brevo), hosting platform (Render — see §9; connect this repo, enter the real secret values `render.yaml` declares by name), Supabase (or chosen Postgres host)
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