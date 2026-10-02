from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from capital_ops.clock import FrozenClock
from capital_ops.errors import Conflict, Forbidden, InvalidState
from capital_ops.service import CapitalSyndicateService


def base_setup(service: CapitalSyndicateService) -> None:
    for user_id, role in (
        ("operator", "operator"),
        ("gp", "gp"),
        ("fof", "fund_of_funds"),
        ("market", "market_fund"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.register_party("operator", {"party_id": "fof", "display_name": "母基金", "kind": "fund_of_funds"})
    service.register_party("operator", {"party_id": "market", "display_name": "市场化基金", "kind": "market_fund"})
    service.publish_round_version("gp", {"round_id": "R", "version": 1, "title": "首轮", "effective_at": "2026-10-01T00:00:00Z", "note": "", "project_id": "drug"})
    service.create_commitment("gp", {"commitment_id": "c-fof", "round_id": "R", "round_version": 1, "party_id": "fof", "amount": "1000"})
    service.create_commitment("gp", {"commitment_id": "c-market", "round_id": "R", "round_version": 1, "party_id": "market", "amount": "500"})
    service.attach_condition("gp", {"condition_id": "k1", "round_id": "R", "scope": "window", "window_id": "w1", "title": "账户开立"})
    service.impose_restriction("gp", {"restriction_id": "r1", "round_id": "R", "window_id": "w1", "title": "仅临床", "purpose": "clinical"})
    service.open_window("gp", {"window_id": "w1", "round_id": "R", "title": "首窗", "opens_at": "2026-10-01T00:00:00Z", "closes_at": "2026-12-31T00:00:00Z", "conditions": ["k1"], "use_restrictions": ["r1"]})


class CapitalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.service = CapitalSyndicateService(self.connection, self.clock)
        base_setup(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def _call(self, call_id, commitment, amount, key):
        return self.service.call_capital("gp", {
            "call_id": call_id, "window_id": "w1", "commitment_id": commitment,
            "amount": amount, "purpose": "clinical", "use_restrictions": ["r1"], "idempotency_key": key,
        })

    def test_party_confirms_only_own_commitment(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.confirm_commitment("market", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        with self.assertRaises(Conflict):
            self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})

    def test_unconfirmed_and_unsatisfied_condition_block_call(self) -> None:
        # 母基金未确认 + 条件未满足
        with self.assertRaises(InvalidState) as ctx:
            self._call("call-1", "c-fof", "100", "key-1")
        codes = {reason["code"] for reason in ctx.exception.details}
        self.assertIn("commitment_not_confirmed", codes)
        self.assertIn("condition_pending", codes)
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        with self.assertRaises(InvalidState) as ctx:
            self._call("call-1", "c-fof", "100", "key-1")
        self.assertEqual({reason["code"] for reason in ctx.exception.details}, {"condition_pending"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        result = self._call("call-1", "c-fof", "100", "key-1")
        self.assertEqual(result["state"], "called")

    def test_idempotent_call_replay(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        first = self._call("call-1", "c-fof", "100", "key-1")
        second = self._call("call-1", "c-fof", "100", "key-1")
        self.assertEqual(first, second)
        changed = {
            "call_id": "call-1", "window_id": "w1", "commitment_id": "c-fof",
            "amount": "200", "purpose": "clinical", "use_restrictions": ["r1"], "idempotency_key": "key-1",
        }
        with self.assertRaises(Conflict):
            self.service.call_capital("gp", changed)
        count = self.connection.execute("SELECT count(*) FROM ledger_events WHERE event_type='capital.called'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_dispute_freezes_only_disputed_amount_others_continue(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.confirm_commitment("market", {"commitment_id": "c-market", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        self._call("call-fof", "c-fof", "200", "kf")
        self._call("call-mkt", "c-market", "100", "km")
        self.service.record_receipt("gp", {"receipt_id": "rf", "call_id": "call-fof", "amount": "200", "received_at": "2026-10-03T00:00:00Z", "reference": "x"})
        self.service.record_receipt("gp", {"receipt_id": "rm", "call_id": "call-mkt", "amount": "100", "received_at": "2026-10-03T00:00:00Z", "reference": "y"})
        # 市场化基金对其中 200 提出争议
        self.service.open_dispute("market", {"dispute_id": "d1", "scope": "commitment", "round_id": "R", "commitment_id": "c-market", "reason": "异议", "amount": "200"})
        # 有争议的后续调用被冻结
        with self.assertRaises(InvalidState) as ctx:
            self._call("call-mkt-2", "c-market", "50", "km2")
        self.assertIn("dispute_frozen", {r["code"] for r in ctx.exception.details})
        # 无争议的母基金继续缴款
        self._call("call-fof-2", "c-fof", "100", "kf2")
        statement = self.service.round_statement("auditor", "R")
        lines = {line["commitment_id"]: line for line in statement["commitments"]}
        self.assertEqual(lines["c-market"]["frozen_amount"], "200.00")
        self.assertEqual(lines["c-market"]["available_to_call"], "200.00")
        self.assertEqual(lines["c-fof"]["frozen_amount"], "0.00")
        self.assertEqual(statement["open_disputes"][0]["dispute_id"], "d1")

    def test_adjustment_is_appended_and_history_kept(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        self._call("call-1", "c-fof", "400", "key-1")
        self.service.append_adjustment("operator", {"adjustment_id": "a1", "kind": "write_off", "round_id": "R", "commitment_id": "c-fof", "amount": "300", "reason": "阶段失败核减"})
        line = self.service.commitment_status("auditor", "c-fof")
        self.assertEqual(line["effective_commitment"], "700.00")
        self.assertEqual(line["called_amount"], "400.00")
        self.assertEqual(line["available_to_call"], "300.00")
        # 不能核减超过未调用余额
        with self.assertRaises(Exception):
            self.service.append_adjustment("operator", {"adjustment_id": "a2", "kind": "write_off", "round_id": "R", "commitment_id": "c-fof", "amount": "400", "reason": "超额"})

    def test_restart_recovers_unresolved_disputes(self) -> None:
        self.service.confirm_commitment("market", {"commitment_id": "c-market", "decision": "confirmed"})
        self.service.open_dispute("market", {"dispute_id": "d1", "scope": "commitment", "round_id": "R", "commitment_id": "c-market", "reason": "异议", "amount": "120"})
        restarted = CapitalSyndicateService(self.connection, self.clock)
        recovered = restarted.recover_unresolved_disputes()
        self.assertEqual(recovered["count"], 1)
        self.assertEqual(recovered["open_disputes"][0]["frozen_amount"], "120.00")
        self.assertTrue(restarted.audit_chain("auditor")["valid"])

    def test_condition_dependency_order(self) -> None:
        self.service.attach_condition("gp", {"condition_id": "k2", "round_id": "R", "scope": "round", "title": "第二轮条件", "depends_on": "k1"})
        with self.assertRaises(InvalidState):
            self.service.satisfy_condition("gp", "k2", "先于依赖")
        self.service.satisfy_condition("gp", "k1", "先满足k1")
        self.service.satisfy_condition("gp", "k2", "再满足k2")

    def test_follow_on_grant_and_exercise(self) -> None:
        self.service.confirm_commitment("market", {"commitment_id": "c-market", "decision": "confirmed"})
        self.service.grant_follow_on("gp", {"follow_on_id": "fo1", "round_id": "R", "holder_party_id": "market", "grant_commitment_id": "c-market", "share_percent": "50", "max_amount": "250", "title": "跟投"})
        # 非持有人不能行使
        with self.assertRaises(Forbidden):
            self.service.exercise_follow_on("fof", {"follow_on_id": "fo1", "amount": "250", "new_commitment_id": "c-fo", "round_version": 1})
        self.service.exercise_follow_on("market", {"follow_on_id": "fo1", "amount": "250", "new_commitment_id": "c-fo", "round_version": 1, "into_window_id": "w1"})
        line = self.service.commitment_status("auditor", "c-fo")
        self.assertEqual(line["committed_amount"], "250.00")
        self.assertEqual(line["status"], "confirmed")
        with self.assertRaises(Conflict):
            self.service.exercise_follow_on("market", {"follow_on_id": "fo1", "amount": "1", "new_commitment_id": "c-fo2", "round_version": 1})

    def test_distribution_cannot_exceed_received_and_payment_respects_freeze(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        self._call("call-1", "c-fof", "300", "key-1")
        self.service.record_receipt("gp", {"receipt_id": "r1", "call_id": "call-1", "amount": "300", "received_at": "2026-10-03T00:00:00Z", "reference": "x"})
        with self.assertRaises(InvalidState):
            self.service.declare_distribution("gp", {"distribution_id": "d1", "round_id": "R", "title": "超额返还", "category": "return_of_capital", "amount": "400", "payable_at": "2026-11-01T00:00:00Z", "allocations": [{"commitment_id": "c-fof", "party_id": "fof", "amount": "400", "source_call_ids": ["call-1"]}]})
        self.service.declare_distribution("gp", {"distribution_id": "d1", "round_id": "R", "title": "返还", "category": "return_of_capital", "amount": "300", "payable_at": "2026-11-01T00:00:00Z", "allocations": [{"commitment_id": "c-fof", "party_id": "fof", "amount": "300", "source_call_ids": ["call-1"]}]})
        # 分配争议冻结支付
        self.service.open_dispute("fof", {"dispute_id": "d2", "scope": "distribution", "round_id": "R", "distribution_id": "d1", "commitment_id": "c-fof", "reason": "金额异议", "amount": "300"})
        with self.assertRaises(InvalidState):
            self.service.record_payment("gp", {"distribution_id": "d1", "commitment_id": "c-fof", "amount": "300", "paid_at": "2026-11-02T00:00:00Z", "reference": "p"})
        self.service.resolve_dispute("operator", {"dispute_id": "d2", "resolution": "released", "note": "解决"})
        self.service.record_payment("gp", {"distribution_id": "d1", "commitment_id": "c-fof", "amount": "300", "paid_at": "2026-11-02T00:00:00Z", "reference": "p"})
        basis = self.service.explain_return("auditor", "d1", "c-fof")
        self.assertEqual(basis["paid_amount"], "300.00")

    def test_closed_round_cannot_be_called(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        self.service.close_round("operator", "R")
        with self.assertRaises(InvalidState) as ctx:
            self._call("call-1", "c-fof", "10", "key-1")
        self.assertIn("round_closed", {r["code"] for r in ctx.exception.details})

    def test_call_dispute_freezes_receipt_and_distribution_source(self) -> None:
        self.service.confirm_commitment("fof", {"commitment_id": "c-fof", "decision": "confirmed"})
        self.service.satisfy_condition("gp", "k1", "已开户")
        self._call("call-1", "c-fof", "300", "key-1")
        # 只实缴 100，其余 200 待缴，就该调用单提出争议
        self.service.record_receipt("gp", {"receipt_id": "r1", "call_id": "call-1", "amount": "100", "received_at": "2026-10-03T00:00:00Z", "reference": "x"})
        self.service.open_dispute("fof", {"dispute_id": "dc1", "scope": "call", "round_id": "R", "call_id": "call-1", "reason": "对该笔调用依据有异议", "amount": "200"})
        # 争议调用单暂停后续实缴
        with self.assertRaises(InvalidState):
            self.service.record_receipt("gp", {"receipt_id": "r2", "call_id": "call-1", "amount": "200", "received_at": "2026-10-04T00:00:00Z", "reference": "y"})
        # 不得以争议调用单为来源宣告返还
        with self.assertRaises(InvalidState):
            self.service.declare_distribution("gp", {"distribution_id": "d1", "round_id": "R", "title": "返还", "category": "return_of_capital", "amount": "100", "payable_at": "2026-11-01T00:00:00Z", "allocations": [{"commitment_id": "c-fof", "party_id": "fof", "amount": "100", "source_call_ids": ["call-1"]}]})
        # 承诺行级冻结不重复计入（争议针对已调用金额）
        line = self.service.commitment_status("auditor", "c-fof")
        self.assertEqual(line["frozen_amount"], "0.00")
        # 重启后仍恢复该争议
        recovered = CapitalSyndicateService(self.connection, self.clock).recover_unresolved_disputes()
        self.assertEqual({d["scope"] for d in recovered["open_disputes"]}, {"call"})


if __name__ == "__main__":
    unittest.main()
