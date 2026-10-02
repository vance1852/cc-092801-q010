"""联合资本承诺与结算的离线验收。

在临时 SQLite 文件库中走完一轮创新药项目联合投资：
母基金/市场化基金/产业方分别确认承诺 → 未满足条件的出资窗口不得调用 →
争议只冻结相关承诺金额、无争议参与方继续出资 → 关闭进程重开后恢复未决冲突 →
项目调整以追加版本记录原因 → 周期台账从有效现金流事件重新计算并给出每笔依据。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from capital_ops.clock import FrozenClock
from capital_ops.errors import InvalidState, SyndicateError
from capital_ops.service import CapitalSyndicateService
from capital_ops.storage import connect


def _revision_one() -> dict[str, object]:
    return {
        "project_id": "proj-anti-pd-1",
        "round_code": "round-a",
        "revision": 1,
        "commitments": [
            {
                "party_id": "fof-1",
                "party_kind": "fof",
                "committed_amount": "600",
                "funding_windows": [
                    {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
                     "closes_on": "2026-06-30", "amount": "300"},
                    {"window_id": "w2", "window_type": "milestone", "opens_on": "2026-07-01",
                     "closes_on": "2026-12-31", "amount": "300"},
                ],
                "conditions": [
                    {"condition_id": "ind-approved", "condition_type": "regulatory",
                     "label": "IND 获得批准", "blocking_window_ids": ["w2"]},
                ],
                "follow_on_rights": [
                    {"right_id": "pro-rata-b", "future_round_code": "round-b",
                     "pro_rata_percent": "50", "exercise_window_days": 30},
                ],
                "restrictions": [
                    {"restriction_id": "clinical-use", "restriction_type": "use",
                     "allowed_categories": ["clinical", "cmc"], "notice_required": True},
                ],
            },
            {
                "party_id": "mf-1",
                "party_kind": "market_fund",
                "committed_amount": "600",
                "funding_windows": [
                    {"window_id": "w1", "window_type": "evergreen", "opens_on": "2026-01-01",
                     "closes_on": "2027-12-31", "amount": "600"},
                ],
                "conditions": [],
                "follow_on_rights": [],
                "restrictions": [
                    {"restriction_id": "no-secondary", "restriction_type": "exit_lock",
                     "allowed_categories": [], "notice_required": False, "note": "24 个月内不得转让份额"},
                ],
            },
            {
                "party_id": "corp-1",
                "party_kind": "corporate",
                "committed_amount": "100",
                "funding_windows": [
                    {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-01-01",
                     "closes_on": "2026-06-30", "amount": "100"},
                ],
                "conditions": [],
                "follow_on_rights": [],
                "restrictions": [],
            },
        ],
    }


def run(workspace: Path) -> dict[str, object]:
    clock = FrozenClock(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="capital-syndicate-") as temporary:
        database = Path(temporary) / "capital.sqlite3"
        connection = connect(database)
        service = CapitalSyndicateService(connection, clock)

        service.create_user("gp", "基金运营", "manager")
        service.create_user("fof-user", "母基金代表", "party", party_id="fof-1")
        service.create_user("mf-user", "市场化基金代表", "party", party_id="mf-1")
        service.create_user("corp-user", "产业方代表", "party", party_id="corp-1")
        service.create_user("corp2-user", "新增产业方代表", "party", party_id="corp-2")
        service.create_user("risk", "风控", "risk")
        service.create_user("audit", "审计", "auditor")

        service.create_project("gp", {"project_id": "proj-anti-pd-1", "name": "抗 PD-1 候选药联合投资"})
        revision_one = service.publish_revision("gp", _revision_one())

        # 多个参与方分别确认自己负责的部分。
        service.confirm_part("fof-user", "proj-anti-pd-1", "fof-1", "commitment", None)
        service.confirm_part("fof-user", "proj-anti-pd-1", "fof-1", "condition", "ind-approved")
        service.confirm_part("mf-user", "proj-anti-pd-1", "mf-1", "window", "w1")
        service.confirm_part("corp-user", "proj-anti-pd-1", "corp-1", "commitment", None)

        # 第一笔调用：w2 的 IND 条件未满足，300 全部被阻，不得调用。
        call_one = service.issue_capital_call("gp", {
            "call_id": "call-2026-q1",
            "project_id": "proj-anti-pd-1",
            "due_on": "2026-03-15",
            "use_category": "clinical",
            "idempotency_key": "call-q1-key",
            "items": [
                {"party_id": "fof-1", "window_code": "w1", "amount": "300"},
                {"party_id": "fof-1", "window_code": "w2", "amount": "300"},
                {"party_id": "mf-1", "window_code": "w1", "amount": "200"},
                {"party_id": "corp-1", "window_code": "w1", "amount": "100"},
            ],
        })
        blocked = {item["window_code"]: item for item in call_one["items"] if item["party_id"] == "fof-1"}
        if blocked["w2"]["state"] != "blocked" or blocked["w2"]["callable_amount"] != "0.00":
            raise RuntimeError("未满足 IND 条件的窗口资金不得调用")

        fof_paid = service.record_cash_event("gp", {
            "event_id": "cash-fof-1", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "300", "occurred_on": "2026-03-10",
            "use_category": "clinical", "idempotency_key": "cash-fof-1-key",
            "note": "首期临床款"})
        # 幂等重放。
        replayed = service.record_cash_event("gp", {
            "event_id": "cash-fof-1", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w1", "amount": "300", "occurred_on": "2026-03-10",
            "use_category": "clinical", "idempotency_key": "cash-fof-1-key", "note": "首期临床款"})
        if replayed["event_id"] != fof_paid["event_id"]:
            raise RuntimeError("出资现金流幂等重放失败")
        # 用途限制：市场费用不在允许类目。
        try:
            service.record_cash_event("gp", {
                "event_id": "cash-bad-use", "project_id": "proj-anti-pd-1", "direction": "contribution",
                "party_id": "fof-1", "window_id": "w1", "amount": "1", "occurred_on": "2026-03-11",
                "use_category": "marketing", "idempotency_key": "bad-use-key"})
            raise RuntimeError("用途限制未被执行")
        except InvalidState:
            pass
        service.record_cash_event("gp", {
            "event_id": "cash-mf-1", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "mf-1", "window_id": "w1", "amount": "200", "occurred_on": "2026-03-12",
            "idempotency_key": "cash-mf-1-key"})
        service.record_cash_event("gp", {
            "event_id": "cash-corp-1", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "corp-1", "window_id": "w1", "amount": "100", "occurred_on": "2026-03-12",
            "idempotency_key": "cash-corp-1-key"})

        # 条件未满足时直接出资 w2 必须被拒绝。
        try:
            service.record_cash_event("gp", {
                "event_id": "cash-fof-early", "project_id": "proj-anti-pd-1", "direction": "contribution",
                "party_id": "fof-1", "window_id": "w2", "amount": "10", "occurred_on": "2026-03-20",
                "idempotency_key": "early-key"})
            raise RuntimeError("未满足条件的窗口不应允许出资")
        except InvalidState:
            pass

        # IND 于 7 月获批，风控记录条件满足，窗口开放。
        clock.current = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
        service.decide_condition("risk", "proj-anti-pd-1", "fof-1", "ind-approved", "satisfied", "IND 批件 CTA-2026-77")

        call_two = service.issue_capital_call("gp", {
            "call_id": "call-2026-q3",
            "project_id": "proj-anti-pd-1",
            "due_on": "2026-09-30",
            "idempotency_key": "call-q3-key",
            "items": [
                {"party_id": "fof-1", "window_code": "w2", "amount": "300"},
                {"party_id": "mf-1", "window_code": "w1", "amount": "300"},
            ],
        })

        # 市场化基金就其中 120 发起争议：只冻结它自己的相关金额。
        dispute = service.raise_dispute("mf-user", {
            "dispute_id": "disp-mf-120", "project_id": "proj-anti-pd-1", "party_id": "mf-1",
            "window_id": "w1", "amount": "120", "reason": "阶段失败后后续份额比例存在分歧"})
        if not dispute["frozen_call_items"]:
            raise RuntimeError("争议应当冻结相关调用条目金额")

        # 无争议的母基金部分继续执行；市场化基金只能缴无争议的 160（280 可调用 - 120 冻结）。
        service.record_cash_event("gp", {
            "event_id": "cash-fof-2", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "fof-1", "window_id": "w2", "amount": "300", "occurred_on": "2026-09-20",
            "idempotency_key": "cash-fof-2-key"})
        service.record_cash_event("gp", {
            "event_id": "cash-mf-2", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "mf-1", "window_id": "w1", "amount": "160", "occurred_on": "2026-09-21",
            "call_id": "call-2026-q3", "idempotency_key": "cash-mf-2-key"})
        try:
            service.record_cash_event("gp", {
                "event_id": "cash-mf-over", "project_id": "proj-anti-pd-1", "direction": "contribution",
                "party_id": "mf-1", "window_id": "w1", "amount": "120", "occurred_on": "2026-09-22",
                "call_id": "call-2026-q3", "idempotency_key": "cash-mf-over-key"})
            raise RuntimeError("争议冻结金额不得在争议解决前被调用")
        except SyndicateError:
            pass

        # 返还：母基金分红 150，按窗口先后冲减净投入；产业方退回 50。
        service.record_cash_event("gp", {
            "event_id": "cash-return-fof", "project_id": "proj-anti-pd-1", "direction": "return",
            "party_id": "fof-1", "return_kind": "distribution", "amount": "150",
            "occurred_on": "2026-10-05", "idempotency_key": "return-fof-key"})
        service.record_cash_event("gp", {
            "event_id": "cash-return-corp", "project_id": "proj-anti-pd-1", "direction": "return",
            "party_id": "corp-1", "return_kind": "refund", "amount": "50",
            "occurred_on": "2026-10-06", "idempotency_key": "return-corp-key"})

        # 模拟进程重启：关闭连接后用新服务重新打开同一 SQLite 文件，恢复未决冲突。
        connection.close()
        restarted = connect(database)
        service = CapitalSyndicateService(restarted, clock)
        recovered = service.recover_pending("audit")
        if recovered["conflict_count"] != 1 or recovered["open_disputes"][0]["dispute_id"] != "disp-mf-120":
            raise RuntimeError("重启后未能恢复尚未解决的承诺冲突")
        if not recovered["frozen_call_items"]:
            raise RuntimeError("重启后未能恢复争议冻结条目")

        # 争议解决：冻结份额恢复，市场化基金补缴 120。
        service.resolve_dispute("risk", "disp-mf-120", "按补充协议确认份额，冻结解除")
        mf_status = service.commitment_status("audit", "proj-anti-pd-1", "mf-1", "2026-10-12")
        if mf_status["dispute_frozen"]["total"] != "0.00":
            raise RuntimeError("争议解决后冻结应清零")
        mf_window = mf_status["funding_windows"][0]["window_code"]
        service.record_cash_event("gp", {
            "event_id": "cash-mf-3", "project_id": "proj-anti-pd-1", "direction": "contribution",
            "party_id": "mf-1", "window_id": "w1", "amount": "120", "occurred_on": "2026-10-12",
            "call_id": "call-2026-q3", "idempotency_key": "cash-mf-3-key"})

        # 项目调整：追加第二版协议（新增产业方），必须留下原因。
        revision_two_payload = {
            "project_id": "proj-anti-pd-1", "round_code": "round-b", "revision": 2,
            "adjustment_kind": "party_add",
            "reason": "阶段临床结果调整后新增产业方 corp-2，原参与方份额不变",
            "commitments": [
                {**_revision_one()["commitments"][0]},  # type: ignore[dict-item]
                {**_revision_one()["commitments"][1]},  # type: ignore[dict-item]
                {**_revision_one()["commitments"][2]},  # type: ignore[dict-item]
                {
                    "party_id": "corp-2", "party_kind": "corporate", "committed_amount": "200",
                    "funding_windows": [
                        {"window_id": "w1", "window_type": "tranche", "opens_on": "2026-11-01",
                         "closes_on": "2027-06-30", "amount": "200"},
                    ],
                    "conditions": [], "follow_on_rights": [], "restrictions": [],
                },
            ],
        }
        revision_two = service.publish_revision("gp", revision_two_payload)
        service.confirm_part("corp2-user", "proj-anti-pd-1", "corp-2", "commitment", None)

        # 周期核算：始终从有效（booked）现金流事件重新计算。
        ledger_full = service.project_ledger("audit", "proj-anti-pd-1")
        ledger_h2 = service.project_ledger("audit", "proj-anti-pd-1", start_on="2026-07-01")
        events = service.cash_events("audit", "proj-anti-pd-1", "fof-1")
        return_basis = next(
            event["basis"] for event in events["events"] if event["event_id"] == "cash-return-fof"
        )
        agreement_v1 = service.agreement("audit", "proj-anti-pd-1", revision=1)
        audit = service.audit_chain("audit")
        connection = restarted
        connection.close()

    if ledger_full["contributions"] != "1180.00" or ledger_full["returns"] != "200.00":
        raise RuntimeError(f"周期合计错误: {ledger_full['contributions']} / {ledger_full['returns']}")
    if ledger_full["net_position"] != "980.00":
        raise RuntimeError("净头寸重算错误")
    if ledger_h2["contributions"] != "580.00":
        raise RuntimeError("周期过滤合计错误")
    if return_basis["allocated_to_windows"][0]["window_code"] != "w1":
        raise RuntimeError("返还必须解释冲减了哪些出资窗口")
    if not audit["valid"]:
        raise RuntimeError("审计哈希链校验失败")
    if revision_one["revision"] != 1 or revision_two["revision"] != 2:
        raise RuntimeError("协议版本追加错误")
    if agreement_v1["state"] != "superseded" or not agreement_v1["addendums"]:
        raise RuntimeError("旧版本应被追加决定取代并保留调整原因")

    return {
        "status": "ok",
        "project": "proj-anti-pd-1",
        "revisions": [revision_one["revision"], revision_two["revision"]],
        "blocked_by_condition": blocked["w2"]["block_reasons"],
        "dispute_frozen_amount": dispute["frozen_call_items"][0]["frozen_amount"],
        "ledger": {"contributions": ledger_full["contributions"], "returns": ledger_full["returns"],
                   "net_position": ledger_full["net_position"]},
        "h2_contributions": ledger_h2["contributions"],
        "return_basis_windows": return_basis["allocated_to_windows"],
        "mf_window": mf_window,
        "recovered_conflicts_after_restart": recovered["conflict_count"],
        "audit_events": audit["events"],
        "audit_valid": audit["valid"],
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行联合资本承诺与结算服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
