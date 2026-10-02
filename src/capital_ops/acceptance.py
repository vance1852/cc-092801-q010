"""联合资本承诺、条件出资、争议冻结与周期结算的离线验收。

在临时内存库中完成多轮协议登记、多方分别确认、条件先后、跟投权、用途限制、
条件未满足不得调用、争议只冻结相关金额、无争议部分继续执行、项目追加调整、
周期对账单重算、现金流依据解释，并在新服务实例上恢复未解决争议，不访问网络。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CapitalSyndicateService
from .storage import inspect_schema


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
    service = CapitalSyndicateService(connection, clock)

    for user_id, role in (
        ("operator", "operator"),
        ("gp", "gp"),
        ("fof", "fund_of_funds"),
        ("market", "market_fund"),
        ("industry", "industrial"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 参与方
    service.register_party("operator", {"party_id": "fof", "display_name": "母基金", "kind": "fund_of_funds"})
    service.register_party("operator", {"party_id": "market", "display_name": "市场化基金", "kind": "market_fund"})
    service.register_party("operator", {"party_id": "industry", "display_name": "产业方", "kind": "industrial"})

    # 每轮协议版本：保存版本历史
    service.publish_round_version("gp", {"round_id": "seed", "version": 1, "title": "首轮共同投资协议", "effective_at": "2026-10-01T00:00:00Z", "note": "初始版本", "project_id": "pcc-2026"})

    # 承诺
    service.create_commitment("gp", {"commitment_id": "com-fof", "round_id": "seed", "round_version": 1, "party_id": "fof", "amount": "6000000"})
    service.create_commitment("gp", {"commitment_id": "com-market", "round_id": "seed", "round_version": 1, "party_id": "market", "amount": "3000000"})
    service.create_commitment("gp", {"commitment_id": "com-industry", "round_id": "seed", "round_version": 1, "party_id": "industry", "amount": "1000000"})

    # 多个参与方分别确认自己负责的部分
    service.confirm_commitment("fof", {"commitment_id": "com-fof", "decision": "confirmed"})
    service.confirm_commitment("market", {"commitment_id": "com-market", "decision": "confirmed"})
    # 产业方暂不确认，验证其份额不被调用

    # 条件先后：监管账户（窗口级）先于 IND 获批（承诺级，依赖前者）
    service.attach_condition("gp", {"condition_id": "cond-escrow", "round_id": "seed", "scope": "window", "window_id": "win-first", "title": "监管资金账户开立"})
    service.attach_condition("gp", {"condition_id": "cond-ind", "round_id": "seed", "scope": "commitment", "commitment_id": "com-industry", "title": "IND 获批", "depends_on": "cond-escrow"})

    # 用途限制
    service.impose_restriction("gp", {"restriction_id": "res-clinical", "round_id": "seed", "window_id": "win-first", "title": "仅用于临床I期", "purpose": "clinical_phase_i"})

    # 出资窗口
    service.open_window("gp", {"window_id": "win-first", "round_id": "seed", "title": "首次缴款窗口", "opens_at": "2026-10-01T00:00:00Z", "closes_at": "2026-12-31T00:00:00Z", "conditions": ["cond-escrow"], "use_restrictions": ["res-clinical"]})

    # 跟投权
    service.grant_follow_on("gp", {"follow_on_id": "fo-market", "round_id": "seed", "holder_party_id": "market", "grant_commitment_id": "com-market", "share_percent": "25", "max_amount": "750000", "window_id": "win-first", "title": "按比例跟投权"})

    # 未满足条件的资金不得被调用
    blocked = service.eligibility_preview("gp", "com-fof", "win-first", "1000000")
    assert not blocked["eligible"]

    service.satisfy_condition("gp", "cond-escrow", "账户编号 ESCROW-1")

    # 无争议、已确认、窗口开启且条件满足：母基金与市场化基金缴款
    service.call_capital("gp", {"call_id": "call-fof-1", "window_id": "win-first", "commitment_id": "com-fof", "amount": "2000000", "purpose": "clinical_phase_i", "use_restrictions": ["res-clinical"], "idempotency_key": "call-fof-1-key"})
    service.call_capital("gp", {"call_id": "call-market-1", "window_id": "win-first", "commitment_id": "com-market", "amount": "1000000", "purpose": "clinical_phase_i", "use_restrictions": ["res-clinical"], "idempotency_key": "call-market-1-key"})
    clock.advance(days=3)
    service.record_receipt("gp", {"receipt_id": "receipt-fof-1", "call_id": "call-fof-1", "amount": "2000000", "received_at": "2026-10-05T00:00:00Z", "reference": "BANK-FOF-0001"})
    service.record_receipt("gp", {"receipt_id": "receipt-market-1", "call_id": "call-market-1", "amount": "1000000", "received_at": "2026-10-05T00:00:00Z", "reference": "BANK-MKT-0001"})

    # 市场化基金对其后续份额提出争议：只冻结相关承诺与金额
    service.open_dispute("market", {"dispute_id": "disp-market", "scope": "commitment", "round_id": "seed", "commitment_id": "com-market", "reason": "对后续缴款节奏存在异议", "amount": "800000", "frozen_share_percent": 100})

    # 无争议部分继续执行：母基金再次缴款
    service.call_capital("gp", {"call_id": "call-fof-2", "window_id": "win-first", "commitment_id": "com-fof", "amount": "1500000", "purpose": "clinical_phase_i", "use_restrictions": ["res-clinical"], "idempotency_key": "call-fof-2-key"})
    clock.advance(days=2)
    service.record_receipt("gp", {"receipt_id": "receipt-fof-2", "call_id": "call-fof-2", "amount": "1500000", "received_at": "2026-10-07T00:00:00Z", "reference": "BANK-FOF-0002"})

    # 项目调整：阶段未达预期，追加核减决定并留下原因，历史不改写
    service.append_adjustment("operator", {"adjustment_id": "adj-fof-1", "kind": "write_off", "round_id": "seed", "commitment_id": "com-fof", "amount": "1000000", "reason": "候选药二期未达主要终点，核减尚未调用的承诺余额"})

    # 产业方满足条件后确认并缴款
    service.satisfy_condition("gp", "cond-ind", "IND 批件号 IND-2026-009")
    service.confirm_commitment("industry", {"commitment_id": "com-industry", "decision": "confirmed"})
    service.call_capital("gp", {"call_id": "call-industry-1", "window_id": "win-first", "commitment_id": "com-industry", "amount": "500000", "purpose": "clinical_phase_i", "use_restrictions": ["res-clinical"], "idempotency_key": "call-industry-1-key"})
    clock.advance(days=1)
    service.record_receipt("gp", {"receipt_id": "receipt-industry-1", "call_id": "call-industry-1", "amount": "500000", "received_at": "2026-10-08T00:00:00Z", "reference": "BANK-IND-0001"})

    # 市场化基金行使跟投权（争议金额冻结，跟投形成新承诺）
    service.exercise_follow_on("market", {"follow_on_id": "fo-market", "amount": "750000", "new_commitment_id": "com-market-fo", "round_version": 1, "into_window_id": "win-first", "note": "行使首轮跟投权"})

    # 分配返还：依据来源调用单
    service.declare_distribution("gp", {"distribution_id": "dist-1", "round_id": "seed", "title": "阶段性本金返还", "category": "return_of_capital", "amount": "600000", "payable_at": "2026-11-01T00:00:00Z", "allocations": [
        {"commitment_id": "com-fof", "party_id": "fof", "amount": "600000", "source_call_ids": ["call-fof-1"]},
    ]})
    service.record_payment("gp", {"distribution_id": "dist-1", "commitment_id": "com-fof", "amount": "600000", "paid_at": "2026-11-02T00:00:00Z", "reference": "RETURN-FOF-0001"})

    # 周期核算：从有效事件重新计算
    statement = service.round_statement("auditor", "seed")

    # 接口解释每笔投入与返还的依据
    contribution_basis = service.explain_contribution("auditor", "receipt-fof-1")
    return_basis = service.explain_return("auditor", "dist-1", "com-fof")

    # 历史时点重算（第二次缴款之前）
    historical = service.round_statement("auditor", "seed", as_of="2026-10-06T00:00:00Z")

    # 重启后恢复尚未解决的承诺冲突
    restarted = CapitalSyndicateService(connection, clock)
    unresolved = restarted.recover_unresolved_disputes()
    chain = restarted.audit_chain("auditor")

    # 争议解决后冻结解除
    service.resolve_dispute("operator", {"dispute_id": "disp-market", "resolution": "split", "note": "部分支持，解冻其余金额"})
    after_resolve = service.round_statement("auditor", "seed")

    result = {
        "status": "ok",
        "schema": inspect_schema(connection),
        "totals": statement["totals"],
        "fof_line": next(line for line in statement["commitments"] if line["commitment_id"] == "com-fof"),
        "market_frozen_before_resolve": next(line for line in statement["commitments"] if line["commitment_id"] == "com-market")["frozen_amount"],
        "market_frozen_after_resolve": next(line for line in after_resolve["commitments"] if line["commitment_id"] == "com-market")["frozen_amount"],
        "follow_on_commitment": service.commitment_status("auditor", "com-market-fo")["committed_amount"],
        "contribution_window": contribution_basis["basis"]["window_id"],
        "contribution_conditions": [item["satisfied"] for item in contribution_basis["basis"]["conditions"]],
        "return_source_calls": return_basis["basis"]["source_calls"],
        "historical_fof_received": next(line for line in historical["commitments"] if line["commitment_id"] == "com-fof")["received_amount"],
        "unresolved_on_restart": unresolved["count"],
        "unresolved_frozen": unresolved["open_disputes"][0]["frozen_amount"] if unresolved["open_disputes"] else None,
        "events_replayed": unresolved["events_replayed"],
        "audit": chain,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行联合资本承诺与结算服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
