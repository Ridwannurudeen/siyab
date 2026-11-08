# Sanctions

ShieldBot reads one sanctions source: the Specially Designated Nationals and Blocked Persons list (the SDN list) of the US Treasury's Office of Foreign Assets Control (OFAC), and from it only the digital currency addresses. The list is applied to the destination recipient of NEAR Intents quotes in the EVM preflight. The NEAR payment checks remain separate because OFAC publishes no NEAR account.

## Source

- Publisher: OFAC, US Department of the Treasury.
- File: `SDN_ADVANCED.XML` from OFAC's Sanctions List Service, https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML. OFAC's technical notice of 16 May 2024 (https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/ofac-technical-actions-in-reverse-chronological-order/20240516_44) names this host for its list files and says it refuses a request without a User-Agent header.
- Terms: a work of the United States government, not subject to copyright in the United States (17 U.S.C. 105); free, with no account. `intel/seeds/raw/NOTICE.md` records the terms.
- Why the advanced XML and not `SDN.CSV`: the CSV carries an entry's digital currency addresses in its remarks column, which stops at 1000 characters, so a long address list is cut. From the publication of 6 Nov 2026, the CSV remarks give 527 listings and 520 distinct addresses under 16 codes, 11 of those listings cut off partway through the address, where `SDN_ADVANCED.XML` gives 1081 listings and 1064 distinct addresses under 20 codes. `SDN.XML` of the same publication, read once as a cross check, gives the same 1081 listings (entry, code, address) with the same programs.

## The extract

`python -m intel sanctions-fetch` (`intel/sanctions.py`) downloads the file, records its URL, fetch time and SHA-256 and the list's own date of issue, and writes `fixtures/sanctions/ofac-sdn-addresses-<fetch time>.json`. The raw file (about 128 MB) is not committed: `intel.sanctions.extract` over a copy whose SHA-256 is the fixture's `source.content_sha256` gives the same listings again. The lookup reads the newest fixture.

Each listing is one Digital Currency Address of one SDN entry, as published:

| Field | Value |
|---|---|
| `uid` | the entry's uid (its `FixedRef`), the number OFAC's other list files give the same entry |
| `name` | the parts of the entry's primary Latin name in the file's order, joined by one space, case unchanged; `SDN.XML` and `SDN.CSV` write the same entry as `LAST, First` with the last name upper cased, and the uid, not the name, identifies it |
| `programs` | the Program codes of the entry's SDN List sanctions entry, such as `CYBER2` or `SDGT` |
| `asset` | OFAC's code after `Digital Currency Address - `, such as `XBT`, `ETH`, `TRX` or `USDT` |
| `address` | the address as published |
| `key` | the comparison key: the address lower cased when it is `0x` and 40 hex characters (an EVM address, whose case is only its checksum), or when it is a bech32 or bech32m form identified by its human readable prefix, separator and data alphabet (case insensitive); the address as published otherwise |

The fixture of the list issued 2026-11-06, fetched 2026-11-07T20:44:17Z (SHA-256 6069...805f), as the command printed it:

1081 listings, 1077 distinct (asset, address) pairs, 1064 distinct addresses, 113 entries, 20 asset codes, NEAR 0; distinct addresses per asset: ARB 1, BCH 7, BNB 1, BSC 1, BSV 1, BTG 1, DASH 5, DOGE 2, ETC 1, ETH 123, LTC 13, SOL 4, TRX 272, USDC 2, USDT 94, XBT 532, XMR 11, XRP 1, XVG 1, ZEC 4

`tests/test_sanctions.py` checks that line, the dates and the shortened hash against the newest fixture, so a new fetch changes this page with it.

## Matching

`intel.sanctions.is_sanctioned(chain_or_asset, address)` reads the fixture, never the network, and returns one match per listing of the address: `uid`, `name`, `programs`, `asset`, `address` as published, `publish_date` (the list's date of issue) and `source_sha256` (the source XML file's SHA-256). The address is compared by its key.

- An upper case argument is an OFAC code (`XBT`, `ETH`, `USDT`) and matches only the listings under that code. A lower case argument is a chain code as 1Click writes it (`eth`, `base`, `btc`, `tron`, `sol`, `near`), so `ETH` and `eth` differ.
- Any chain code but `near` matches every listing whose key equals the address's key, whatever code OFAC filed it under, and the match names that code. The address is what the list names: one key holds the same EVM address on every EVM chain, Omni USDT and XBT share Bitcoin addresses, and the list itself files one Tron shaped address under XBT. These are kept as published.
- `near` matches only a listing under a code `NEAR`, and the list has none, so no NEAR account matches: not a 64 hex implicit account (one XMR listing is 64 hex characters), and not an account written like an EVM address.

## NEAR Intents destinations

For an EVM recipient written as `0x` plus 40 hex characters, the Intents check compares its normalized key against every listed digital-currency address, regardless of whether OFAC publishes an asset code for the destination chain. This follows the list's address identity: one EVM address is the same holder on each EVM chain. Other recipient formats use only the OFAC asset codes mapped to the destination chain. The configured asset-code mappings are `eth` (ETH), `arb` (ARB), `bsc` (BNB and BSC), `btc` (XBT), `sol` (SOL), `dash` (DASH), `doge` (DOGE), `xrp` (XRP), `zec` (ZEC), `tron` (TRX), `ltc` (LTC) and `bch` (BCH).

The 1Click chain codes with no address code in this SDN publication are `near`, `base`, `ton`, `gnosis`, `bera`, `pol`, `sui`, `movement`, `op`, `avax`, `stellar`, `aptos`, `cardano`, `xlayer`, `monad`, `adi`, `plasma`, `scroll`, `starknet`, `aleo`, `hypercore`, `fogo`, `hood` and `hlevm`. An EVM-shaped recipient on any of these chains is still checked by normalized key across all listed addresses. For a non-EVM recipient, the check passes with detail saying the list publishes no address for that chain. A missing or unreadable fixture is UNKNOWN. Its evidence source names the committed fixture and hashes its JSON bytes; a match reason carries the source XML hash.

## What a match means

A sanctions match is a fact with a date, never a score: no confidence, no threshold, no weighing against other checks. For an Intents destination, a match means DENY, and the reason names the SDN entry (its uid, name and programs), the code and the address as OFAC lists them, the list's date of issue and the source XML file's SHA-256. No match says only that the list of that date does not name the address; it passes nothing else.

As of the fetch of 2026-11-07T20:44:17Z, the list issued 2026-11-06 has no NEAR code and no NEAR account: none of its 1064 distinct addresses is on NEAR.
