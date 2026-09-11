# Manual Test Packs

Four intake scenarios for manually exercising `/proposals/new`, each covering a
different combination of field-completeness and attachments. Nothing here
touches the database or any script - copy the fields from a pack's
`intake.md` into the New Proposal form by hand, attach the files listed (if
any), and generate.

| Pack | Fields | Attachment | What it exercises |
| --- | --- | --- | --- |
| [pack-1-complete-with-attachment](pack-1-complete-with-attachment/) | Complete | 1 file (`.md`) | Normal happy path with a reference file feeding generation |
| [pack-2-complete-no-attachment](pack-2-complete-no-attachment/) | Complete | None | Normal happy path, form fields only |
| [pack-3-incomplete-no-attachment](pack-3-incomplete-no-attachment/) | Thin (only the required fields filled) | None | The `has_gap` path - Claude should flag missing sections rather than invent detail, and Approve should be blocked until they're resolved |
| [pack-4-incomplete-with-transcript-and-spec](pack-4-incomplete-with-transcript-and-spec/) | Thin (same as pack 3) | 2 files: a call transcript (`.txt`) + a requirements spec (`.md`) | Whether generation actually pulls substance from attached reference files to fill gaps the form fields left thin, instead of surfacing the same gaps as pack 3 |

Required fields per `app/routers/proposals.py`: **Representative** (the
individual contact, `client_name`), **Client/Company** (`company_name`), and
**Representative Email** (`client_email`, email-format checked) are always
required, along with **Date of Call** (which also can't be a future date -
a discovery call can't have happened yet). The six narrative fields (needs
summary, scope, goals, services, timeline, pricing) are required too -
*unless* at least one reference file is already attached, in which case they
can be left genuinely blank and a **Fill In From Reference Documents**
button appears (skipping those fields' `required` attribute) to generate
straight from what's attached. This is why pack 3 (no attachment) still
needs a thin filler sentence in each narrative field to get past validation,
while pack 4 (has an attachment) can leave them completely empty and use
that button instead of "Create Proposal."
