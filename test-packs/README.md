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

Required fields per `app/routers/proposals.py` (`REQUIRED_TEXT_FIELDS` + the
email regex check) are only **Client Name**, **Client Email**, and **Company
Name** - every other field can be submitted blank, which is exactly what
packs 3 and 4 do on purpose.
