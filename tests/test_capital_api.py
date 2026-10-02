from __future__ import annotations

import json
import sqlite3
import unittest

from capital_ops.api import JsonApplication
from capital_ops.service import CapitalSyndicateService


class CapitalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(CapitalSyndicateService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path, payload, actor="operator"):
        return self.app.handle(
            "POST", path, {"x-actor-id": actor}, json.dumps(payload).encode("utf-8")
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self.app.handle("POST", "/parties", {}, b"{}")
        self.assertEqual(response.status, 422)

    def test_party_and_round_flow(self) -> None:
        self.assertEqual(self._post("/users", {"user_id": "operator", "display_name": "运营", "role": "operator"}).status, 201)
        party = self._post("/parties", {"party_id": "fof", "display_name": "母基金", "kind": "fund_of_funds"})
        self.assertEqual(party.status, 201)
        published = self._post("/rounds/versions", {"round_id": "R", "version": 1, "title": "首轮", "effective_at": "2026-10-01T00:00:00Z", "note": "", "project_id": "drug"}, actor="gp")
        # gp 尚未创建 -> 404/403 类错误（这里 gp 用户不存在）
        self.assertIn(published.status, (403, 404))
        self._post("/users", {"user_id": "gp", "display_name": "GP", "role": "gp"})
        published = self._post("/rounds/versions", {"round_id": "R", "version": 1, "title": "首轮", "effective_at": "2026-10-01T00:00:00Z", "note": "", "project_id": "drug"}, actor="gp")
        self.assertEqual(published.status, 201)

    def test_ineligible_call_returns_structured_reasons(self) -> None:
        self._post("/users", {"user_id": "operator", "display_name": "运营", "role": "operator"})
        self._post("/users", {"user_id": "gp", "display_name": "GP", "role": "gp"})
        self._post("/users", {"user_id": "fof", "display_name": "母基金", "role": "fund_of_funds"})
        self._post("/parties", {"party_id": "fof", "display_name": "母基金", "kind": "fund_of_funds"})
        self._post("/rounds/versions", {"round_id": "R", "version": 1, "title": "首轮", "effective_at": "2026-10-01T00:00:00Z", "note": "", "project_id": "drug"}, actor="gp")
        self._post("/commitments", {"commitment_id": "c1", "round_id": "R", "round_version": 1, "party_id": "fof", "amount": "1000"}, actor="gp")
        self._post("/conditions", {"condition_id": "k1", "round_id": "R", "scope": "window", "window_id": "w1", "title": "开户"}, actor="gp")
        self._post("/restrictions", {"restriction_id": "r1", "round_id": "R", "window_id": "w1", "title": "临床", "purpose": "clinical"}, actor="gp")
        self._post("/windows", {"window_id": "w1", "round_id": "R", "title": "窗口", "opens_at": "2026-10-01T00:00:00Z", "closes_at": "2026-12-31T00:00:00Z", "conditions": ["k1"], "use_restrictions": ["r1"]}, actor="gp")
        self._post("/commitments/confirmations", {"commitment_id": "c1", "decision": "confirmed"}, actor="fof")
        called = self._post("/capital_calls", {"call_id": "call1", "window_id": "w1", "commitment_id": "c1", "amount": "100", "purpose": "clinical", "use_restrictions": ["r1"], "idempotency_key": "key1"}, actor="gp")
        self.assertEqual(called.status, 409)
        self.assertEqual(called.body["error"]["code"], "invalid_state")
        codes = [item["code"] for item in called.body["error"]["details"]]
        self.assertIn("condition_pending", codes)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "operator"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
