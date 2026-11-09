# Intear reference adapter

This is a reference adapter in our repository. It maps a Siyab answer to Safe, Unsafe or Unknown and creates the JSON body for a proposed `GET /addresssafety?address=<account>` route. A wallet operator could host that route beside `/dappsafety` on their own service.

Reference code in our repository. Not reviewed, adopted or acknowledged by Intear. No contact made as of 8 Nov. Their wallet code is not modified. This adapter has not been tested against their service.

`state_for_verdict` maps ALLOW to Safe and DENY to Unsafe. ASK and unreadable verdicts map to Unknown. A NEAR answer with action `ask` stays Unknown even when its outcome is ALLOW. `address_safety_json` returns the `status`, `reasons` and `evidence_hash` fields as JSON. It does not start an HTTP server or add address detection logic.

The badge trusts the operator's own server, verifies no signature, and binds each verdict to the address shown.
