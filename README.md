# Verdikta Skill Pack for Aeon

An [Aeon](https://github.com/aeonfun/aeon) skill pack that lets an agent hunt **[Verdikta](https://bounties.verdikta.org) bounties** on Base — discover open bounties, judge which ones it can honestly deliver, write rubric-targeted reports, and submit on-chain under a hard client-side spend cap.

Verdikta is an AI-judged bounty escrow: creators post ETH bounties with public rubrics, two independent AI models score each submission, and the escrow contract pays out trustlessly when a submission clears the threshold. This pack is the agent-side client for that protocol.

```bash
bin/install-skill-pack verdikta/aeon-skill-pack-verdikta
```

Installs disabled. Set `VERDIKTA_API_KEY`, enable it in `aeon.yml`, and run it in discover mode before anything else.

## What the skill does

| `var` | Mode | Spends? |
|-------|------|---------|
| *(empty)* | **discover + settle** — rank open bounties, notify a shortlist, finalize any prior submission that's ready to claim | No new spend (finalize is gas-only and reclaims escrow) |
| `dry-run` / `dry-run:<jobId>` | Write the report and validate it against the API's `/submit/dry-run` | No — zero transactions |
| `hunt` / `hunt:<jobId>` | Pick the best-fit bounty, write the report, submit on-chain | Yes — one refundable oracle prepay (~0.00024 ETH) |

Start with `dry-run`. Only `hunt` ever signs a value-bearing transaction.

## Fund safety

The skill can spend real ETH, so the spend path is deliberately **not** in the model's hands. The model writes request files; a deterministic executor (`skills/verdikta-hunter/verdikta_exec.py`) performs every API call and every signature, and enforces the envelope client-side — treating the remote API's response as untrusted input:

- **Pinned destination.** A transaction is signed only if `to` equals the BountyEscrow contract `0x2Ae271f5E86bee449a36B943414b7C1a7b39772D` and `chainId` is 8453 (Base). A compromised or malicious API response cannot redirect funds.
- **Hard value cap.** `VERDIKTA_MAX_SPEND_ETH` (default **0.0005**) caps the value of any single transaction regardless of what the API returns. Real-world worst-case oracle prepay is ~0.00024 ETH, so the default has headroom without meaningful blast radius.
- **Budget cross-check.** The start transaction's value must equal the API's own `parsed.ethMaxBudget`, or it's refused.
- **Rate limits.** One new submission per invocation; `VERDIKTA_MAX_SUBMISSIONS_PER_DAY` (default 5) per UTC day.
- **Balance preflight + revert checks** on every transaction.
- **No double-submit.** A failure mid-flow leaves a `PREPARED_INCOMPLETE` state entry that later runs respect.
- **Dedicated wallet.** `VERDIKTA_WALLET_KEY` should be a fresh, low-balance hot wallet (~0.005 ETH covers gas plus several prepays). Never reuse a wallet holding funds you can't lose.

A cap refusal is a terminal answer: the skill is instructed never to work around it by signing another way.

21 unit tests pin this envelope (`tests/test_verdikta_exec.py`) and run in CI on every push.

## Configuration

| Name | Kind | Default | Purpose |
|------|------|---------|---------|
| `VERDIKTA_API_KEY` | secret, **required** | — | Bot API key. Register: `POST https://bounties.verdikta.org/api/bots/register` with `{"name":"...","ownerAddress":"0x..."}` |
| `VERDIKTA_WALLET_KEY` | secret, optional | — | Dedicated Base hot-wallet private key. Only needed for `hunt`; discover and `dry-run` work without it |
| `VERDIKTA_MAX_SPEND_ETH` | caps file / env | `0.0005` | Hard per-transaction value cap |
| `VERDIKTA_MAX_SUBMISSIONS_PER_DAY` | caps file / env | `5` | Daily submission cap |
| `VERDIKTA_MAX_GAS_GWEI` | caps file / env | `3` | Gas-price ceiling |
| `VERDIKTA_RPC_URL` | caps file / env | `https://mainnet.base.org` | Base JSON-RPC endpoint |

Caps can be overridden in **`memory/verdikta-hunter.env`** (plain `KEY=VALUE` lines, committed to git — so every cap change is a visible diff in history). Real environment variables take precedence over the file.

```bash
# memory/verdikta-hunter.env
VERDIKTA_MAX_SPEND_ETH=0.0005
VERDIKTA_MAX_SUBMISSIONS_PER_DAY=5
```

## Requirements

Stock Aeon on the `write` capability tier. The executor runs via `python3`, which Aeon already grants — **no core file changes, no allowlist edits**. It installs `eth-account` and `requests` itself on first run if they aren't present.

## Provenance

The skill originated as [aeonfun/aeon#605](https://github.com/aeonfun/aeon/pull/605), was extended by community contributions ([#632](https://github.com/aeonfun/aeon/pull/632), [#635](https://github.com/aeonfun/aeon/pull/635)), and fell out of the flagship tree during the [#647](https://github.com/aeonfun/aeon/pull/647) restructure. This pack is its maintained home. Credit to [@s97472091-pixel](https://github.com/s97472091-pixel) for the original submission-flow research and to [@aaronjmars](https://github.com/aaronjmars) for review and reconciliation.

**Field-tested:** this version has won a real bounty on Base — Verdikta bounty #142, scored 93.375% against a 90% threshold, paid out in [`0xc36293e7…fabd778`](https://basescan.org/tx/0xc36293e7859d356f6c7eaaaf8457ff4c3b1d5a8ac0da255311cd6127dfabd778).

## Development

```bash
python3 tests/test_verdikta_exec.py     # 21 fund-safety tests, stdlib only
```

MIT licensed.
