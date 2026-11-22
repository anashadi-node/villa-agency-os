# PACK-005C C6 — Supplier Signer Authorization Design (Proposal Only)

**Status:** proposal for Owner decision. This file does **not** change
`src/roles.py`, `src/identity.py`, `src/supplier_assertion.py`, tenant policy, or
the effective authorization policy. ADR-0074 remains **PROPOSED**; G2-B and
supplier enforcement remain OFF.

## 1. Observed conflict

The signer is bound to `reservation_manager`:

- `src/roles.py`: `SIGNER_ROLES = ("reservation_manager",)`.
- `src/supplier_assertion.py`: `CONTACT_REGISTRY_ROLES = ("owner", "gm")` and
  `CONTACT_READER_ROLES = tuple(sorted(set(CONTACT_REGISTRY_ROLES) | set(SIGNER_ROLES)))`.
- The callback read calls `_resolve_principal(..., step_up=False)`, but
  `_resolve_principal` requires `mfa_satisfied_at` before returning a principal.
- `src/identity.py::open_session` only prompts for a TOTP when the role is in
  `MFA_REQUIRED_ROLES` (and in the tenant's `security.mfa.required_roles` policy).
- `MFA_REQUIRED_ROLES` currently has `owner`, `finance_manager`, and `gm`; it does
  not include `reservation_manager`.

Therefore a `reservation_manager` can be a legitimate signer, but a fresh session
never satisfies the prerequisite that the contact-read path requires. The observed
`reservation_manager → MFA_REQUIRED → supplier contact-read unavailable` is a
**policy-set mismatch**, not a missing role-holder and not a reason to bypass MFA.

## 2. Options compared

### Option A — make the signer an MFA-required role

Change the default tuple and require the same value in each tenant's
`security.mfa.required_roles` policy:

```python
MFA_REQUIRED_ROLES = ("owner", "finance_manager", "gm", "reservation_manager")
```

- Session opening for a reservation_manager requires TOTP.
- Signing still needs a fresh step-up (the existing signing check remains).
- The contact-read path works because the same role now satisfies the MFA gate.
- Cost: every reservation_manager session gets MFA, including non-signing work.
- Security effect: stronger; one less exception path; no new permission.

### Option B — separate masked read and mandatory step-up for signing

Add a narrow `supplier_contact:read_masked` permission for reservation_manager,
returning a masked number without MFA, and require step-up before signing or
before revealing the full callback number.

- More granular; non-signing reads stay low-friction.
- Cost: a new permission, a new masked-read API, migration/policy changes, and a
  second place where full-contact release must be checked.
- Risk: the signer cannot call the supplier with a masked number; a full read
  still needs step-up, so the original flow remains gated. More surface area for
  an accidental unmasked response.

## 3. Recommendation

**Option A.** It is the smaller, safer change and matches the already-approved
principle that a human signer is a high-trust role. It preserves:

- authenticated role-holder binding;
- maker/checker separation (`CONTACT_REGISTRY_ROLES` remains owner/GM);
- tenant-scoped authorization;
- MFA for sensitive actions;
- fresh step-up for signing;
- audit events;
- no privilege escalation and no owner-token workaround.

Option B is not recommended for this pack because it adds a permission and a
masked-contact API to solve a policy-set mismatch that Option A closes directly.

## 4. Proposed patch and tests (not applied)

The code change is one tuple value, plus a tenant policy data update (owner
approval required; not a repository source edit):

```diff
-MFA_REQUIRED_ROLES = ("owner", "finance_manager", "gm")
+MFA_REQUIRED_ROLES = ("owner", "finance_manager", "gm", "reservation_manager")
```

Tests to run after approval:

- `tests/test_supplier_signer_policy.py` — the strict xfail flips to XPASS and
  must be updated to a normal passing assertion.
- existing supplier-assertion tests: a reservation_manager without MFA cannot
  sign; with fresh MFA can read the contact and sign; owner/GM maker/checker
  remains unchanged.
- tenant isolation: a reservation_manager in agency A cannot read agency B's
  contact.
- audit: denied and allowed reads/signings produce the expected events.
- no owner-token path: owner cannot sign on behalf of a reservation manager.

## 5. Activation gates

1. Owner records the policy decision (and any tenant policy values).
2. Owner accepts or amends ADR-0074; this proposal does not accept it.
3. Migration `020` and the contact triggers are authorized separately.
4. G2-B and supplier enforcement remain OFF until their own approval.
5. No live permission, timer, payment, or deployment change follows from this
   document.
