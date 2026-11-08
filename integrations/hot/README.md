# HOT reference preflight

This reference preflight delegates NEAR transfers to `gate.verdict.request` and destination chain payouts to `gate.intents.judge_quote`. It adds no detection logic. The NEAR path passes no signer and requests no signature. The Intents path is already unsigned.

HOT signs omni addresses. The caller must pass the real recipient carried in the intent, not the omni wrapper. For an Intents payout, pass the origin asset, destination asset, amount and actual destination recipient. The destination asset carries the chain information used by the existing Intents checks.

Parsing HOT's intent format is NOT built. No HOT SDK is used and no HOT code is copied. The caller must extract and bind the real transfer fields before calling this module.

Reference code in our repository. Not reviewed, adopted or acknowledged by HOT. No contact made as of 8 Nov. This preflight has not been tested with HOT.
