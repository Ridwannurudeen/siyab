# ShieldBot for NEAR: security intelligence for agent money, enforced on chain.

Who your agent pays gets a verdict with evidence. Unknown never passes.

Build in progress. The full README lands on 10 Nov.

## Limits

- NearBlocks: the free tier allows 6 calls a minute and 333 a day. `gate/rpc.py` spaces NearBlocks fetches at least 10 s apart and stops at 300 a day per process; a check that cannot read NearBlocks reads Unknown. Fixtures are reused for 24 hours. Data: NearBlocks.
