from __future__ import annotations

import unittest
from pathlib import Path

from capital_ops.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class CapitalAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["revisions"], [1, 2])
        self.assertEqual(result["ledger"]["contributions"], "1180.00")
        self.assertEqual(result["ledger"]["returns"], "200.00")
        self.assertEqual(result["ledger"]["net_position"], "980.00")
        self.assertEqual(result["recovered_conflicts_after_restart"], 1)
        self.assertTrue(result["audit_valid"])
        self.assertIn("condition:ind-approved:pending", result["blocked_by_condition"])


if __name__ == "__main__":
    unittest.main()
