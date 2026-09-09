# BrightPath Logistics - Current Systems Overview

Prepared by the BrightPath ops team ahead of the discovery call, for reference.

## Systems in use today

- **TMS (transport management system):** Cargofly, hosted, used by dispatchers to log every shipment as it's booked.
- **Shared spreadsheet:** a Google Sheet the ops team maintains in parallel, used for internal status tracking that Cargofly doesn't expose well (driver assignment notes, customer-specific handling instructions).
- **Invoicing tool:** QuickBooks Online, updated manually once a shipment is marked delivered.

## The actual pain

Every shipment currently gets keyed into all three systems by hand, separately, by whichever dispatcher is free. That means:

- The same shipment reference number is typed three times, by three different people, on three different days - a clear source of typos and mismatches.
- When a customer calls asking "where's my shipment," staff have to check whichever of the three systems happens to be most up to date, which isn't always the same one.
- Invoicing is sometimes delayed by 2-3 days because updating QuickBooks is the last step in the chain and gets deprioritized when dispatchers are busy.

## What "good" looks like to us

One place a dispatcher enters a shipment, and the other two systems reflect it automatically - with a clear alert if something doesn't match (e.g. a shipment marked delivered in Cargofly but missing from the spreadsheet).
