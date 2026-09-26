"""The §21 deny registry must name the tools that exist.

§21 denies a list of capabilities to AI. ``ai_service`` enforces that list by comparing the **tool
name** against the registry entries verbatim, so a registry entry naming an action that no tool
carries denies nothing at all. That is exactly what happened to ``discount.approve``: the registry
named it, the catalogue registered ``discount.approve_out_of_policy``, and the DENY matched neither
— the capability stayed reachable by any future allowance that happened to grant it.

These tests make that class of drift impossible to reintroduce quietly: every §21 capability must
be denied, every tool that carries one must be named, and no entry may exist that denies nothing.
"""

from __future__ import annotations

import unittest

import policy_engine as PE
import reservation_kernel as RK
from primitives import FrozenClock, IdFactory

AGENCY = "agency_default"
NOW = "2026-09-26T08:00:00+00:00"
REGISTRY = "security.ai.denied_actions"

#: §21's list, as the spec writes it.
SPEC_DENIALS: tuple[str, ...] = (
    "payment.approve",
    "payment.execute",
    "refund.approve",
    "refund.execute",
    "bank.change",
    "bank.approve",
    "discount.approve",
    "margin_override.approve",
    "contract.activate",
    "rate.verify",
    "rate.activate",
    "permission.change",
    "audit.modify",
    "audit.delete",
    "secret.read",
    "passport.read_raw",
)

#: Where the catalogue registers the same capability under another name, the registry has to name
#: the *tool*: enforcement compares tool names, not intents. Every §21 entry a tool carries belongs
#: here — the mapping is what keeps a rename from silently retiring a denial.
SPEC_DENIAL_TOOLS: dict[str, str] = {
    "discount.approve": "discount.approve_out_of_policy",
}

#: Beyond §21, the owner added these (ADR-0009 "base list + amendment"). They name capabilities
#: rather than registered tools, so they guard the action vocabulary — listed here so a new entry
#: cannot appear in the registry without a reader noticing it.
OWNER_EXTENSIONS: tuple[str, ...] = (
    "blockchain.sign",
    "crypto.transfer",
    "withdrawal.approve",
    "treasury.control",
    "key.read_raw",
    "seed_phrase.read",
)


class DenyRegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = RK.ReservationKernel.open(
            ":memory:",
            clock=FrozenClock.at(NOW),
            ids=IdFactory(deterministic=True, seed="deny-registry"),
            agency_id=AGENCY,
        )
        self.addCleanup(self.kernel.close)
        self.resolver = PE.PolicyResolver(
            self.kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY
        )
        self.published = self.resolver.resolve(REGISTRY)
        self.denied = set(self.published.as_json())
        self.tools = {
            row["tool"] for row in self.kernel.conn.execute("SELECT tool FROM ai_tools").fetchall()
        }

    def test_the_registry_denies_every_capability_the_spec_names(self) -> None:
        missing = sorted(name for name in SPEC_DENIALS if name not in self.denied)
        self.assertEqual(missing, [], f"§21 names these; the registry does not: {missing}")

    def test_the_registry_denies_the_tool_that_carries_the_capability(self) -> None:
        """The regression this wave fixes: a denial must name the tool the catalogue registered."""
        for spec_name, tool in SPEC_DENIAL_TOOLS.items():
            self.assertIn(tool, self.tools, f"{tool} carries {spec_name}; it must be a real tool")
            self.assertIn(tool, self.denied, f"the registry must deny {tool}, not only {spec_name}")

    def test_both_discount_approvals_are_denied_to_the_machine(self) -> None:
        """§11 sends an out-of-policy discount to a human; §21 keeps the machine out of it."""
        self.assertIn("discount.approve_out_of_policy", self.denied)
        self.assertIn("margin_override.approve", self.denied)

    def test_a_denied_tool_is_registered_high_risk(self) -> None:
        rows = {
            row["tool"]: row["tier"]
            for row in self.kernel.conn.execute("SELECT tool, tier FROM ai_tools").fetchall()
        }
        for tool in SPEC_DENIAL_TOOLS.values():
            self.assertEqual(rows.get(tool), "HIGH", f"{tool} must stay HIGH risk")

    def test_every_registry_entry_is_accounted_for(self) -> None:
        """An entry matching no tool and no §21 capability is a denial that guards nothing."""
        accounted = set(SPEC_DENIALS) | set(SPEC_DENIAL_TOOLS.values()) | set(OWNER_EXTENSIONS)
        unexplained = sorted(self.denied - accounted)
        self.assertEqual(
            unexplained, [], f"registry entries nobody can account for: {unexplained}"
        )

    def test_the_amendment_is_the_revision_in_force(self) -> None:
        """The v2 amendment must be what resolution returns, not the v1 list it replaces."""
        self.assertEqual(self.published.version, 2)
        self.assertEqual(self.published.effective_from, "2026-09-26")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
