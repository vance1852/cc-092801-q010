from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from capital_ops.api import JsonApplication
from capital_ops.clock import FrozenClock
from capital_ops.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from capital_ops.service import CapitalSyndicateService
from capital_ops.storage import connect


def _commitment(
    party_id: str = "fof-1",
    *,
    committed: str = "300",
    windows: list | None = None,
    conditions: list | None = None,
    restrictions: list | None = None,
    rights: list | None = None,
) -> dict:
    return {
        "party_id": party_id,
        "party_kind": "fof",
        "committed_amount": committed,
        "funding_windows": windows or [
            {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
             "closes_on": "2026-06-30", "amount": committed},
        ],
        "conditions": conditions or [],
        "follow_on_rights": rights or [],
        "restrictions": restrictions or [],
    }


def _revision(commitments: list[dict] | None = None, *, revision: int = 1, round_code: str = "round-a") -> dict:
    return {
        "project_id": "proj-1",
        "round_code": round_code,
        "revision": revision,
        "commitments": commitments if commitments is not None else [_commitment()],
    }


class CapitalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
        self.service = CapitalSyndicateService(self.connection, self.clock)
        for user_id, role, party_id in (
            ("gp", "manager", None),
            ("fof-user", "party", "fof-1"),
            ("mf-user", "party", "mf-1"),
            ("risk", "risk", None),
            ("audit", "auditor", None),
        ):
            self.service.create_user(user_id, user_id, role, party_id=party_id)
        self.service.create_project("gp", {"project_id": "proj-1", "name": "早期项目"})

    def tearDown(self) -> None:
        self.connection.close()

    def _publish(self, commitments: list[dict] | None = None) -> dict:
        return self.service.publish_revision("gp", _revision(commitments))

    # ---------- 协议与确认 ----------

    def test_window_total_must_equal_commitment(self) -> None:
        bad = _commitment(committed="300", windows=[
            {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
             "closes_on": "2026-06-30", "amount": "200"},
        ])
        with self.assertRaises(ValidationFailed):
            self._publish([bad])

    def test_condition_must_reference_existing_window(self) -> None:
        bad = _commitment(conditions=[
            {"condition_id": "c1", "condition_type": "clinical", "label": "二期入组",
             "blocking_window_ids": ["w9"]},
        ])
        with self.assertRaises(ValidationFailed):
            self._publish([bad])

    def test_party_can_only_confirm_own_parts(self) -> None:
        self._publish()
        with self.assertRaises(Forbidden):
            self.service.confirm_part("mf-user", "proj-1", "fof-1", "commitment", None)
        self.service.confirm_part("fof-user", "proj-1", "fof-1", "commitment", None)
        with self.assertRaises(Conflict):
            self.service.confirm_part("fof-user", "proj-1", "fof-1", "commitment", None)

    def test_second_revision_requires_reason_and_supersedes(self) -> None:
        first = self._publish()
        with self.assertRaises(ValidationFailed):
            self.service.publish_revision("gp", _revision(revision=2, round_code="round-b"))
        payload = _revision(revision=2, round_code="round-b")
        payload["adjustment_kind"] = "party_exit"
        payload["reason"] = "阶段失败，市场化基金退出后续份额"
        second = self.service.publish_revision("gp", payload)
        self.assertEqual(second["revision"], 2)
        old = self.service.agreement("audit", "proj-1", revision=1)
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["addendums"][0]["adjustment_kind"], "party_exit")
        del first

    # ---------- 条件门控 ----------

    def test_unsatisfied_condition_blocks_call_and_cash(self) -> None:
        commitment = _commitment(committed="300", windows=[
            {"window_id": "w1", "window_type": "milestone", "opens_on": "2026-01-01",
             "closes_on": "2026-12-31", "amount": "300"},
        ], conditions=[
            {"condition_id": "ind", "condition_type": "regulatory", "label": "IND 批准",
             "blocking_window_ids": ["w1"]},
        ])
        self._publish([commitment])
        call = self.service.issue_capital_call("gp", {
            "call_id": "call-1", "project_id": "proj-1", "due_on": "2026-03-15",
            "idempotency_key": "k1",
            "items": [{"party_id": "fof-1", "window_code": "w1", "amount": "300"}]})
        item = call["items"][0]
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["callable_amount"], "0.00")
        self.assertTrue(any("condition:ind" in reason for reason in item["block_reasons"]))
        with self.assertRaises(InvalidState):
            self.service.record_cash_event("gp", {
                "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
                "party_id": "fof-1", "window_id": "w1", "amount": "10",
                "occurred_on": "2026-03-10", "idempotency_key": "e1k"})
        self.service.decide_condition("risk", "proj-1", "fof-1", "ind", "satisfied", "批件已下")
        self.service.record_cash_event("gp", {
            "event_id": "e2", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "300",
            "occurred_on": "2026-03-11", "idempotency_key": "e2k"})

    def test_window_closed_outside_window_dates(self) -> None:
        self._publish()
        call = self.service.issue_capital_call("gp", {
            "call_id": "call-1", "project_id": "proj-1", "due_on": "2026-08-15",
            "idempotency_key": "k1",
            "items": [{"party_id": "fof-1", "window_code": "w1", "amount": "300"}]})
        self.assertEqual(call["items"][0]["state"], "blocked")
        self.assertIn("window:w1:closed", call["items"][0]["block_reasons"])

    # ---------- 用途限制 ----------

    def test_use_restriction_rejects_unlisted_category(self) -> None:
        commitment = _commitment(restrictions=[
            {"restriction_id": "r1", "restriction_type": "use",
             "allowed_categories": ["clinical"], "notice_required": False},
        ])
        self._publish([commitment])
        with self.assertRaises(InvalidState):
            self.service.record_cash_event("gp", {
                "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
                "party_id": "fof-1", "window_id": "w1", "amount": "10",
                "occurred_on": "2026-02-01", "use_category": "marketing",
                "idempotency_key": "e1k"})

    # ---------- 争议部分冻结 ----------

    def _two_parties_published(self) -> None:
        self.service.publish_revision("gp", _revision([
            _commitment("fof-1", committed="300"),
            {
                "party_id": "mf-1", "party_kind": "market_fund", "committed_amount": "300",
                "funding_windows": [
                    {"window_id": "w1", "window_type": "evergreen", "opens_on": "2026-01-01",
                     "closes_on": "2027-12-31", "amount": "300"}],
                "conditions": [], "follow_on_rights": [], "restrictions": [],
            },
        ]))

    def test_dispute_freezes_only_related_party(self) -> None:
        self._two_parties_published()
        self.service.issue_capital_call("gp", {
            "call_id": "call-1", "project_id": "proj-1", "due_on": "2026-03-15",
            "idempotency_key": "k1",
            "items": [
                {"party_id": "fof-1", "window_code": "w1", "amount": "300"},
                {"party_id": "mf-1", "window_code": "w1", "amount": "300"},
            ]})
        dispute = self.service.raise_dispute("mf-user", {
            "dispute_id": "d1", "project_id": "proj-1", "party_id": "mf-1",
            "window_id": "w1", "amount": "100", "reason": "份额比例争议"})
        self.assertEqual(dispute["frozen_call_items"][0]["frozen_amount"], "100.00")
        # 无争议的母基金全额继续执行。
        self.service.record_cash_event("gp", {
            "event_id": "e-fof", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "300",
            "occurred_on": "2026-03-10", "idempotency_key": "e-fof-k"})
        # 争议方只能缴未冻结的 200。
        self.service.record_cash_event("gp", {
            "event_id": "e-mf-1", "project_id": "proj-1", "direction": "contribution",
            "party_id": "mf-1", "window_id": "w1", "amount": "200",
            "occurred_on": "2026-03-11", "call_id": "call-1",
            "idempotency_key": "e-mf-1-k"})
        with self.assertRaises((InvalidState, Conflict)):
            self.service.record_cash_event("gp", {
                "event_id": "e-mf-2", "project_id": "proj-1", "direction": "contribution",
                "party_id": "mf-1", "window_id": "w1", "amount": "100",
                "occurred_on": "2026-03-12", "call_id": "call-1",
                "idempotency_key": "e-mf-2-k"})
        # 争议解除后可以补缴。
        self.service.resolve_dispute("risk", "d1", "确认比例")
        self.service.record_cash_event("gp", {
            "event_id": "e-mf-3", "project_id": "proj-1", "direction": "contribution",
            "party_id": "mf-1", "window_id": "w1", "amount": "100",
            "occurred_on": "2026-03-13", "call_id": "call-1",
            "idempotency_key": "e-mf-3-k"})

    def test_party_cannot_dispute_other_party(self) -> None:
        self._two_parties_published()
        with self.assertRaises(Forbidden):
            self.service.raise_dispute("fof-user", {
                "dispute_id": "d1", "project_id": "proj-1", "party_id": "mf-1",
                "amount": "10", "reason": "越权争议"})

    # ---------- 现金流、返还与台账 ----------

    def test_return_cannot_exceed_net_contribution(self) -> None:
        self._publish()
        self.service.record_cash_event("gp", {
            "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "100",
            "occurred_on": "2026-02-01", "idempotency_key": "e1k"})
        with self.assertRaises(Conflict):
            self.service.record_cash_event("gp", {
                "event_id": "r1", "project_id": "proj-1", "direction": "return",
                "party_id": "fof-1", "return_kind": "refund", "amount": "150",
                "occurred_on": "2026-03-01", "idempotency_key": "r1k"})

    def test_ledger_recomputes_from_booked_events_only(self) -> None:
        self._publish()
        self.service.record_cash_event("gp", {
            "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "100",
            "occurred_on": "2026-02-01", "idempotency_key": "e1k"})
        self.service.record_cash_event("gp", {
            "event_id": "r1", "project_id": "proj-1", "direction": "return",
            "party_id": "fof-1", "return_kind": "distribution", "amount": "40",
            "occurred_on": "2026-03-01", "idempotency_key": "r1k"})
        ledger = self.service.project_ledger("audit", "proj-1")
        self.assertEqual(ledger["contributions"], "100.00")
        self.assertEqual(ledger["returns"], "40.00")
        self.assertEqual(ledger["net_position"], "60.00")
        # 冲销出资后，台账只从 booked 事件重算，净头寸随之变化。
        self.service.reverse_cash_event("gp", "e1", "凭证作废")
        ledger_after = self.service.project_ledger("audit", "proj-1")
        self.assertEqual(ledger_after["contributions"], "0.00")
        self.assertEqual(ledger_after["net_position"], "-40.00")

    def test_return_basis_explains_window_allocation(self) -> None:
        commitment = _commitment(committed="300", windows=[
            {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
             "closes_on": "2026-06-30", "amount": "100"},
            {"window_id": "w2", "window_type": "milestone", "opens_on": "2026-07-01",
             "closes_on": "2026-12-31", "amount": "200"},
        ])
        self._publish([commitment])
        self.service.decide_condition  # 风控存在即可；w2 无条件
        self.service.record_cash_event("gp", {
            "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "100",
            "occurred_on": "2026-02-01", "idempotency_key": "e1k"})
        returned = self.service.record_cash_event("gp", {
            "event_id": "r1", "project_id": "proj-1", "direction": "return",
            "party_id": "fof-1", "return_kind": "distribution", "amount": "80",
            "occurred_on": "2026-03-01", "idempotency_key": "r1k"})
        self.assertEqual(returned["basis"]["allocated_to_windows"],
                         [{"window_code": "w1", "amount": "80.00"}])

    def test_idempotent_cash_replay(self) -> None:
        self._publish()
        payload = {
            "event_id": "e1", "project_id": "proj-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "100",
            "occurred_on": "2026-02-01", "idempotency_key": "same-key"}
        first = self.service.record_cash_event("gp", payload)
        second = self.service.record_cash_event("gp", payload)
        self.assertEqual(first["event_id"], second["event_id"])
        count = self.connection.execute("SELECT count(*) FROM cash_events").fetchone()[0]
        self.assertEqual(count, 1)

    # ---------- 重启恢复 ----------

    def test_restart_recovers_open_dispute(self) -> None:
        self._two_parties_published()
        self.service.issue_capital_call("gp", {
            "call_id": "call-1", "project_id": "proj-1", "due_on": "2026-03-15",
            "idempotency_key": "k1",
            "items": [{"party_id": "mf-1", "window_code": "w1", "amount": "300"}]})
        self.service.raise_dispute("mf-user", {
            "dispute_id": "d1", "project_id": "proj-1", "party_id": "mf-1",
            "window_id": "w1", "amount": "300", "reason": "阶段性失败，出资义务存疑"})
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "restart.sqlite3")
            backup = sqlite3.connect(path)
            self.connection.backup(backup)
            backup.close()
            restarted = connect(path)
            service = CapitalSyndicateService(restarted, self.clock)
            recovered = service.recover_pending("audit", "proj-1")
            self.assertEqual(recovered["conflict_count"], 1)
            self.assertEqual(recovered["open_disputes"][0]["dispute_id"], "d1")
            self.assertTrue(recovered["frozen_call_items"])
            restarted.close()


class CapitalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(CapitalSyndicateService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self.app.handle("GET", "/projects/p1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_project_and_revision_flow(self) -> None:
        self.app.handle("POST", "/users", headers={"x-actor-id": "system"}, body=json.dumps(
            {"user_id": "gp", "display_name": "gp", "role": "manager"}).encode())
        response = self.app.handle(
            "POST", "/projects", headers={"x-actor-id": "gp"},
            body=json.dumps({"project_id": "p1", "name": "项目"}).encode())
        self.assertEqual(response.status, 201)
        revision = {
            "project_id": "p1", "round_code": "round-a", "revision": 1,
            "commitments": [{
                "party_id": "fof-1", "party_kind": "fof", "committed_amount": "100",
                "funding_windows": [
                    {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
                     "closes_on": "2026-12-31", "amount": "100"}],
                "conditions": [], "follow_on_rights": [], "restrictions": []}],
        }
        created = self.app.handle(
            "POST", "/agreement_revisions", headers={"x-actor-id": "gp"},
            body=json.dumps(revision).encode())
        self.assertEqual(created.status, 201)
        ledger = self.app.handle(
            "GET", "/projects/p1/ledger", headers={"x-actor-id": "gp"})
        self.assertEqual(ledger.status, 200)
        self.assertEqual(ledger.body["net_position"], "0.00")


if __name__ == "__main__":
    unittest.main()
