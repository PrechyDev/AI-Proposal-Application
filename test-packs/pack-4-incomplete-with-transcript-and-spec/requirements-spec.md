# Harborline Freight Co. - Customs Documentation Requirements (internal note)

Shared by Marcus Webb after the call, from Harborline's ops team.

## Document types involved

1. **Commercial invoice** - itemized list of goods, unit prices, and line totals, issued by the supplier.
2. **Packing list** - physical contents of each shipment (weights, dimensions, box counts).
3. **Customs declaration** - the document actually submitted to customs; every line-item total on it must reconcile exactly with the commercial invoice, or the shipment is flagged for manual review at the port.

## Supplier document formats received today

- ~60% arrive as PDF (scanned or exported)
- ~30% arrive as Excel/CSV
- ~10% arrive as plain text in the body of an email

## Must-haves for a workable solution

- Extract line items (description, quantity, unit price, total) from whatever format a supplier document arrives in.
- Cross-check extracted totals against each other before a customs declaration is generated - flag any mismatch instead of submitting it.
- Produce a customs declaration in the format Harborline's freight software already accepts (a structured CSV import - format available on request, not attached here).

## Explicitly out of scope for this phase

- Automating communication with customs authorities directly - Harborline's own staff still handle submission.
- Supplier-side changes - whatever is built has to work with documents exactly as suppliers already send them today.

## Timing

Harborline's next import season starts in roughly six weeks from the September 8 call; ideally this is live and handling real shipments before that ramp-up begins.
