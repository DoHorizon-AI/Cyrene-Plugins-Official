# Bilibili connector tests

Tests use the real provider and signed shared `ToolEnvelope` contracts with
deterministic local HTTP peers. They verify operation allowlists, actor and
profile binding, QR secrecy, account lease redemption, mutation receipts,
idempotent replays, unknown write outcomes, and fixed-host HTTP behavior.
No test uses a Bilibili account or treats an external account action as passed.
