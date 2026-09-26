# ADR-0013 — Stablecoin (USDC/USDT) controls

- Status: **ACCEPTED (design) — capability GATED** (owner decision, 2026-09-26)
- Accepted as the internal model. Activation remains blocked until a recorded compliance review
  (jurisdiction, sanctions/AML/KYC, tax/accounting, custody/treasury, refund policy) is attached
  to this ADR. Designing the controls now is required; accepting funds is not.
- Date: 2026-09-26
- Spec: Master Spec v1.1 §60 (**LOCKED**), §14, §21, §48, §49

## Context

v1.1 allows USDC/USDT acceptance as an *architectural capability*, and simultaneously locks a
set of controls so crypto never weakens human financial authority. Activation depends on
compliance decisions that the spec explicitly leaves OPEN (§49): jurisdiction, sanctions/AML/KYC,
tax/accounting treatment, custody/treasury, refund policy.

This ADR decides the **internal model**; it does not claim the capability is activated.

## Decision

1. **Asset and network are separately allowlisted.** Token symbol alone is insufficient:
   a row identifies (`asset`, `network`, `contract_address`) and a payment is only valid against
   an allowlisted pair. An unknown or mismatched network/token is an exception, never a settlement.
2. **Payment instructions carry** asset, network, amount/pricing rule, destination, reference,
   expiry where applicable, and the confirmation/finality policy (how many confirmations count).
3. **Verification sources are ranked and explicit**: approved custody/payment provider, trusted
   chain data, or controlled human verification. No screenshot-based settlement, ever.
4. **Exception catalogue is modelled, not improvised**: underpayment, overpayment, duplicate
   transaction, wrong token, wrong network, late payment, finality/reorg, provider outage,
   unmatched transaction. Each maps to a reconciliation case with an owner and a deadline — the
   same pattern already used for client money.
5. **Wallet substitution is treated as bank-substitution risk**: a wallet/address change is a
   versioned change request with out-of-band verification and human approval (§14 flow), and an
   approved payment is invalidated if its destination changes.
6. **Key material never enters the system's reach**: private keys, seed phrases, and any signing
   or withdrawal credential never appear in the database, LLM context, frontend, logs, source
   code, or ordinary staff access. Treasury signing stays outside this platform.
7. **AI may never** sign a blockchain transaction, transfer crypto, approve a withdrawal, or
   control a treasury wallet — implemented as *no such capability exists*, not as a prompt
   instruction.
8. **Activation is gated** on a recorded compliance review; until then the rail reports
   `SOURCE_UNAVAILABLE`-style state rather than accepting funds.

## Alternatives considered

- **Accept and settle inside the platform** — rejected: it puts key custody and treasury inside
  the application, maximising the highest-severity risk in §48 for no business need.
- **Treat any token on any network ("symbol only")** — rejected: wrong-network transfers are
  unrecoverable and would be recorded as money.
- **Screenshot confirmation** — rejected by §60 explicitly.

## Security & operational impact

- Adds a high-value target (destination integrity) whose control is the existing bank-change
  approval flow — reuse, not a parallel mechanism.
- On-chain reorgs mean "confirmed" is a policy value, not a constant; it must live in the Policy
  Engine (§16), not in code.

## Migration / reversibility

Additive columns/table for allowlisted (asset, network). Deactivation is a config change: the
rail refuses new intents while historical rows stay readable.

## Tests required

- Wrong network / wrong token / wrong amount → exception, no settlement.
- Destination change after approval → approval invalidated, no silent redirect.
- Duplicate transaction → one financial entry.
- AI attempts to sign/transfer/approve withdrawal → capability absent (adversarial suite).
- Screenshot submitted as evidence → not accepted as settlement.
