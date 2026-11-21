# NearBlocks parity captures

Public NearBlocks reads captured on 21 Nov 2026. Of 69 transaction comparisons, 67 match every consumed field and two have v1 floating point deposit precision loss. Ten keys accounts were compared against both the committed v1 recording and current v1. Those comparisons expose timestamp precision, changed key histories, a tied page boundary and growth in listed keys. The exact differences are retained in `differences.json`.

All 105 public GET requests returned HTTP 200. `manifest.json` binds the 89 primary responses to their request URLs, HTTP statuses, read times and SHA256 of the exact server bytes. `initial-captures/manifest.json` binds the other 16 responses, which were archived and repeated with more precise timing. Both sets live outside every replay fixture directory.

The manifests retain the original SHA256 of all 175 committed NearBlocks files: 69 v1 transaction recordings, 104 v1 keys recordings and two v3 account transaction pages. All 175 files remain byte for byte unchanged. The transaction reads cover all 69 recordings, including their repeated transaction URLs. Ten mainnet keys samples have current v1 and v3 responses.

The two transaction differences are amounts in yoctoNEAR:

| Capture | V1 consumed deposit | Exact v3 deposit | Difference |
| --- | --- | --- | --- |
| `txn-59-v3.json` | 121076455956140050000000000 | 121076455956140052029494512 | 2029494512 |
| `txn-62-v3.json` | 59453923452133320000000000 | 59453923452133320795572005 | 795572005 |

Every other consumed transaction field matches. The exact v3 digits are preserved.

The keys comparisons have 277 field differences across the recorded and current v1 comparisons, including the derived oldest creation:

| Category | Field differences | Observed behavior |
| --- | --- | --- |
| Timestamp precision | 217 | The same creation or deletion transaction has a v1 numeric timestamp and a v3 digit string differing by at most 175 ns. |
| Creation and deletion history | 24 | Three `intents.near` keys have v1 creation events from 5 Nov 2024 and deletion events from Jan 2025. V3 gives later creations from 19 May or 2 Jun 2025 without those earlier deletions. |
| Tied full page membership | 4 | `user.intear.near` has one different key in its 25-key pages. The two keys share a creation transaction and timestamp. The existing full page unknown rule remains required. |
| Historical listed key changes | 32 | `grid.deltatrade.near` grows from 7 to 9 listed keys, `mmt-arb.near` from 5 to 12 and `via-trading.near` from 2 to 22. Current v1 and v3 counts agree. |

The history differences include creation timestamps about 195 to 209 days apart. They are classified as different events, separately from timestamp precision. The empty `rhea000453.multica.near` response matches both its recorded and current v1 response. All nine other sampled accounts have at least a precision difference.

`collect.py` uses the public HTTP client in off mode. Its normal NearBlocks spacing, daily ceiling and provider budget still apply. No key, cursor or limit is sent. Every primary capture stores its monotonic request start; the smallest measured interval is 11.046 seconds. The initial 16 responses stored wall time only, with logged intervals down to 10.989 seconds. Their exact eleven second spacing was not established, so their bytes and original provenance remain in `initial-captures/` and all 16 were repeated for the primary set. Resuming the script preserves completed captures and refuses a changed recording inventory.

Run the offline checks from the repository root:

```
python -m unittest tests.test_nearblocks_parity -v
```

The comparison covers key counts, public keys, permission kinds and creation and deletion events, and each transaction's hash, signer, receiver, block height, deposit, outcome status and action fields. Key fields are grouped by public key. The first minimum timestamp creation in page order is also compared, preserving the tie selection used by the rules and cluster reader. Transaction deposits use the same decimal interpretation as the rules. Function call arguments are compared as decoded objects, as the creation check reads them.

`differences.json` lists every observed difference with its exact v1 and v3 values, preserving null, empty and all-null events separately. Regression tests require every observed difference to equal that reviewed list, prove that both differing v1 deposits equal the floating point conversion of the exact v3 integers, and allow only deposit paths in the transaction differences. The final run reports 13 passing tests, with no failure or skip.
