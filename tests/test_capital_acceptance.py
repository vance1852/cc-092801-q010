from __future__ import annotations

import unittest
from pathlib import Path

from capital_ops.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class CapitalAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema"]["missing_tables"], [])
        # 争议只冻结市场化基金相关金额，母基金不受影响
        self.assertEqual(result["market_frozen_before_resolve"], "800000.00")
        self.assertEqual(result["market_frozen_after_resolve"], "0.00")
        # 项目调整只追加、不改写历史：有效承诺被核减
        self.assertEqual(result["fof_line"]["effective_commitment"], "5000000.00")
        self.assertEqual(result["fof_line"]["committed_amount"], "6000000.00")
        # 跟投权形成已确认的新承诺
        self.assertEqual(result["follow_on_commitment"], "750000.00")
        # 周期核算可从历史时点重算
        self.assertEqual(result["historical_fof_received"], "2000000.00")
        # 重启后恢复尚未解决的承诺冲突
        self.assertEqual(result["unresolved_on_restart"], 1)
        self.assertEqual(result["unresolved_frozen"], "800000.00")
        # 每笔投入与返还都能解释依据
        self.assertEqual(result["contribution_window"], "win-first")
        self.assertTrue(all(result["contribution_conditions"]))
        self.assertEqual(result["return_source_calls"][0]["call_id"], "call-fof-1")
        # 哈希链完整
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
