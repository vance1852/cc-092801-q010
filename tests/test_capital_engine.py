from __future__ import annotations

import unittest
from decimal import Decimal

from capital_ops import engine


def event(seq, event_type, payload, actor="gp", created_at="2026-10-02T08:00:00Z", entity_id=None):
    return {
        "seq": seq,
        "event_type": event_type,
        "entity_type": "x",
        "entity_id": entity_id or str(seq),
        "actor_id": actor,
        "payload": payload,
        "previous_hash": "0",
        "event_hash": "0",
        "created_at": created_at,
    }


def build_round():
    return [
        event(1, "party.registered", {"party_id": "p1", "kind": "market_fund"}),
        event(2, "round.version_published", {"round_id": "R", "project_id": "drug", "version": 1, "title": "t", "effective_at": "2026-10-01T00:00:00Z"}),
        event(3, "commitment.created", {"commitment_id": "c1", "round_id": "R", "round_version": 1, "party_id": "p1", "amount": "1000", "currency": "CNY"}),
        event(4, "commitment.confirmed", {"commitment_id": "c1", "party_id": "p1", "decision": "confirmed", "confirmed_amount": "1000"}),
        event(5, "condition.attached", {"condition_id": "k1", "round_id": "R", "scope": "window", "window_id": "w1", "title": "条件", "blocking": True}),
        event(6, "window.opened", {"window_id": "w1", "round_id": "R", "title": "窗口", "opens_at": "2026-10-01T00:00:00Z", "closes_at": "2026-12-31T00:00:00Z", "conditions": ["k1"], "use_restrictions": []}),
    ]


class EngineTests(unittest.TestCase):
    def test_replay_builds_state(self) -> None:
        state = engine.replay(build_round())
        self.assertEqual(state["commitments"]["c1"]["decision"], "confirmed")
        self.assertFalse(state["conditions"]["k1"]["satisfied"])

    def test_unsatisfied_condition_blocks_call(self) -> None:
        state = engine.replay(build_round())
        result = engine.call_eligibility(
            state, commitment_id="c1", window_id="w1", amount=Decimal("100"), as_of="2026-10-02T08:00:00Z"
        )
        self.assertFalse(result["eligible"])
        self.assertIn("condition_pending", [reason["code"] for reason in result["reasons"]])

    def test_window_timing_blocks(self) -> None:
        state = engine.replay(build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}, created_at="2026-10-02T08:00:00Z"),
        ])
        before = engine.call_eligibility(state, commitment_id="c1", window_id="w1", amount=Decimal("10"), as_of="2026-09-30T00:00:00Z")
        self.assertIn("window_not_open", [r["code"] for r in before["reasons"]])
        after = engine.call_eligibility(state, commitment_id="c1", window_id="w1", amount=Decimal("10"), as_of="2027-01-01T00:00:00Z")
        self.assertIn("window_closed", [r["code"] for r in after["reasons"]])

    def test_dispute_freezes_only_target_commitment(self) -> None:
        events = build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}),
            event(8, "commitment.created", {"commitment_id": "c2", "round_id": "R", "round_version": 1, "party_id": "p1", "amount": "500", "currency": "CNY"}),
            event(9, "commitment.confirmed", {"commitment_id": "c2", "party_id": "p1", "decision": "confirmed", "confirmed_amount": "500"}),
            event(10, "dispute.opened", {"dispute_id": "d1", "scope": "commitment", "round_id": "R", "commitment_id": "c1", "amount": "400", "frozen_share_percent": "100", "reason": "争议"}),
        ]
        state = engine.replay(events)
        frozen_c1 = engine.call_eligibility(state, commitment_id="c1", window_id="w1", amount=Decimal("10"), as_of="2026-10-03T00:00:00Z")
        clear_c2 = engine.call_eligibility(state, commitment_id="c2", window_id="w1", amount=Decimal("10"), as_of="2026-10-03T00:00:00Z")
        self.assertIn("dispute_frozen", [r["code"] for r in frozen_c1["reasons"]])
        self.assertTrue(clear_c2["eligible"])
        line = engine.commitment_line(state, "c1")
        self.assertEqual(line["frozen_amount"], "400.00")
        self.assertEqual(line["available_to_call"], "600.00")
        # 解决争议后解冻
        state_resolved = engine.replay(events + [
            event(11, "dispute.resolved", {"dispute_id": "d1", "resolution": "released"}, created_at="2026-10-04T00:00:00Z"),
        ])
        self.assertEqual(engine.commitment_line(state_resolved, "c1")["frozen_amount"], "0.00")

    def test_partial_freeze_share(self) -> None:
        events = build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}),
            event(8, "dispute.opened", {"dispute_id": "d1", "scope": "commitment", "round_id": "R", "commitment_id": "c1", "amount": "500", "frozen_share_percent": "50", "reason": "部分争议"}),
        ]
        state = engine.replay(events)
        self.assertEqual(engine.commitment_line(state, "c1")["frozen_amount"], "250.00")

    def test_statement_totals_and_as_of(self) -> None:
        events = build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}, created_at="2026-10-02T09:00:00Z"),
            event(8, "capital.called", {"call_id": "call1", "round_id": "R", "window_id": "w1", "commitment_id": "c1", "amount": "300", "purpose": "x", "use_restriction_ids": []}, created_at="2026-10-02T10:00:00Z"),
            event(9, "capital.received", {"receipt_id": "r1", "call_id": "call1", "commitment_id": "c1", "amount": "300", "received_at": "2026-10-02T10:00:00Z"}, created_at="2026-10-02T10:00:00Z"),
        ]
        current = engine.round_statement(engine.replay(events), "R")
        self.assertEqual(current["totals"]["called_amount"], "300.00")
        historical = engine.round_statement(engine.replay(events, as_of="2026-10-02T09:30:00Z"), "R")
        self.assertEqual(historical["totals"]["called_amount"], "0.00")

    def test_adjustment_changes_effective_commitment(self) -> None:
        events = build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}),
            event(8, "capital.called", {"call_id": "call1", "round_id": "R", "window_id": "w1", "commitment_id": "c1", "amount": "400", "purpose": "x", "use_restriction_ids": []}),
            event(9, "adjustment.appended", {"adjustment_id": "a1", "scope": "commitment", "round_id": "R", "commitment_id": "c1", "kind": "write_off", "amount": "300", "reason": "阶段失败"}),
        ]
        line = engine.commitment_line(engine.replay(events), "c1")
        self.assertEqual(line["effective_commitment"], "700.00")
        self.assertEqual(line["available_to_call"], "300.00")

    def test_explain_receipt_and_payment(self) -> None:
        events = build_round() + [
            event(7, "condition.satisfied", {"condition_id": "k1"}),
            event(8, "capital.called", {"call_id": "call1", "round_id": "R", "window_id": "w1", "commitment_id": "c1", "amount": "300", "purpose": "临床", "use_restriction_ids": []}),
            event(9, "capital.received", {"receipt_id": "r1", "call_id": "call1", "commitment_id": "c1", "amount": "300", "received_at": "2026-10-05T00:00:00Z"}),
            event(10, "distribution.declared", {"distribution_id": "d1", "round_id": "R", "category": "return_of_capital", "amount": "100", "title": "返还", "payable_at": "2026-11-01T00:00:00Z", "allocations": [{"commitment_id": "c1", "party_id": "p1", "amount": "100", "source_call_ids": ["call1"], "paid_amount": "0"}]}),
            event(11, "distribution.paid", {"distribution_id": "d1", "commitment_id": "c1", "party_id": "p1", "amount": "100", "paid_at": "2026-11-02T00:00:00Z"}),
        ]
        state = engine.replay(events)
        receipt = engine.explain_receipt(state, "r1")
        self.assertEqual(receipt["basis"]["call_id"], "call1")
        self.assertEqual(receipt["basis"]["conditions"][0]["satisfied"], True)
        payment = engine.explain_payment(state, "d1", "c1")
        self.assertEqual(payment["paid_amount"], "100.00")
        self.assertEqual(payment["basis"]["source_calls"][0]["call_id"], "call1")


if __name__ == "__main__":
    unittest.main()
