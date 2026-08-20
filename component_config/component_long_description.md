Extracts e-shop data from Shoptet through the official Shoptet REST API — the API-based
successor to the Shoptet Permalink data source, which downloads CSV exports from permalink
URLs.

Covers orders (with line items), products (with variants, images and parameters), customers,
stock, accounting documents (invoices, proforma invoices, credit notes, delivery notes,
proofs of payment), abandoned carts, marketing objects and the full set of reference data —
over fifty objects in all, one per configuration row.

Bulk collections are read through Shoptet's asynchronous snapshot exports, so a full order or
product history arrives in one pass with the same detail as the per-record endpoints.
Incremental loads ask Shoptet only for records changed since the previous run and upsert them
by primary key; dedicated change feeds expose edits and deletions.

Authentication works either with a private API token generated in the e-shop administration
(Shoptet Premium) or with an addon's OAuth access token (Shoptet API partners).
