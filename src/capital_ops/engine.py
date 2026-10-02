"""从仅追加事件序列重放联合资本状态的纯函数引擎。

引擎不触碰数据库、不读取时钟：给定有序事件即可得到完全确定的结果，
因此周期核算、争议冻结金额与现金流依据都能在重启后从有效事件重新计算。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from .ledger import ZERO, money_text, quantize_money


def _money(value: object) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


def initial_state() -> dict[str, Any]:
    return {
        "parties": {},
        "rounds": {},
        "commitments": {},
        "windows": {},
        "conditions": {},
        "restrictions": {},
        "follow_ons": {},
        "calls": {},
        "receipts": [],
        "distributions": {},
        "payments": [],
        "disputes": [],
        "adjustments": [],
        "last_event_at": None,
        "events_applied": 0,
    }


def replay(events: Sequence[Mapping[str, Any]], as_of: str | None = None) -> dict[str, Any]:
    """按序号重放事件；as_of 给出时只采用不晚于该时刻的事件。"""

    state = initial_state()
    ordered = sorted(events, key=lambda event: int(event["seq"]))
    for event in ordered:
        created_at = str(event["created_at"])
        if as_of is not None and created_at > as_of:
            continue
        _apply(state, event)
        state["last_event_at"] = created_at
        state["events_applied"] += 1
    return state


def _apply(state: dict[str, Any], event: Mapping[str, Any]) -> None:
    event_type = event["event_type"]
    payload = event["payload"]
    if isinstance(payload, str):  # 容忍尚未反序列化的载荷
        import json

        payload = json.loads(payload)
    handler = _HANDLERS.get(event_type)
    if handler is not None:
        handler(state, event, payload)


def _register_party(state, event, payload) -> None:
    state["parties"][payload["party_id"]] = {
        "party_id": payload["party_id"],
        "display_name": payload.get("display_name", payload["party_id"]),
        "kind": payload.get("kind", "lp"),
        "registered_at": event["created_at"],
    }


def _publish_round(state, event, payload) -> None:
    round_id = payload["round_id"]
    round_record = state["rounds"].setdefault(
        round_id,
        {"round_id": round_id, "project_id": payload.get("project_id"), "versions": [], "closed_at": None},
    )
    round_record["project_id"] = payload.get("project_id", round_record.get("project_id"))
    round_record["versions"].append(
        {
            "version": int(payload["version"]),
            "title": payload.get("title", ""),
            "effective_at": payload["effective_at"],
            "note": payload.get("note", ""),
            "supersedes_version": payload.get("supersedes_version"),
            "published_at": event["created_at"],
            "published_by": event["actor_id"],
        }
    )


def _create_commitment(state, event, payload) -> None:
    state["commitments"][payload["commitment_id"]] = {
        "commitment_id": payload["commitment_id"],
        "round_id": payload["round_id"],
        "round_version": payload.get("round_version"),
        "party_id": payload["party_id"],
        "amount": _money(payload["amount"]),
        "currency": payload.get("currency", "CNY"),
        "note": payload.get("note", ""),
        "created_at": event["created_at"],
        "decision": "pending",
        "confirmed_amount": ZERO,
        "confirmations": [],
    }


def _open_window(state, event, payload) -> None:
    state["windows"][payload["window_id"]] = {
        "window_id": payload["window_id"],
        "round_id": payload["round_id"],
        "title": payload.get("title", ""),
        "opens_at": payload["opens_at"],
        "closes_at": payload["closes_at"],
        "condition_ids": list(payload.get("conditions", [])),
        "use_restriction_ids": list(payload.get("use_restrictions", [])),
        "opened_at": event["created_at"],
    }


def _attach_condition(state, event, payload) -> None:
    state["conditions"][payload["condition_id"]] = {
        "condition_id": payload["condition_id"],
        "round_id": payload["round_id"],
        "scope": payload.get("scope", "window"),
        "window_id": payload.get("window_id"),
        "commitment_id": payload.get("commitment_id"),
        "title": payload.get("title", ""),
        "blocking": bool(payload.get("blocking", True)),
        "satisfied": False,
        "satisfied_at": None,
        "evidence": None,
        "depends_on": payload.get("depends_on"),
    }


def _satisfy_condition(state, event, payload) -> None:
    condition = state["conditions"][payload["condition_id"]]
    condition["satisfied"] = True
    condition["satisfied_at"] = payload.get("satisfied_at", event["created_at"])
    condition["evidence"] = payload.get("evidence")


def _grant_follow_on(state, event, payload) -> None:
    state["follow_ons"][payload["follow_on_id"]] = {
        "follow_on_id": payload["follow_on_id"],
        "round_id": payload["round_id"],
        "holder_party_id": payload["holder_party_id"],
        "grant_commitment_id": payload.get("grant_commitment_id"),
        "share_percent": _money(payload.get("share_percent", 0)),
        "max_amount": None if payload.get("max_amount") is None else _money(payload["max_amount"]),
        "window_id": payload.get("window_id"),
        "expires_at": payload.get("expires_at"),
        "title": payload.get("title", ""),
        "granted_at": event["created_at"],
        "exercised": False,
        "exercise": None,
    }


def _exercise_follow_on(state, event, payload) -> None:
    follow_on = state["follow_ons"][payload["follow_on_id"]]
    follow_on["exercised"] = True
    follow_on["exercise"] = {
        "amount": _money(payload["amount"]),
        "new_commitment_id": payload.get("new_commitment_id"),
        "into_window_id": payload.get("into_window_id"),
        "note": payload.get("note", ""),
        "exercised_at": event["created_at"],
    }


def _impose_restriction(state, event, payload) -> None:
    state["restrictions"][payload["restriction_id"]] = {
        "restriction_id": payload["restriction_id"],
        "round_id": payload["round_id"],
        "window_id": payload.get("window_id"),
        "commitment_id": payload.get("commitment_id"),
        "purpose": payload.get("purpose", ""),
        "title": payload.get("title", ""),
    }


def _confirm_commitment(state, event, payload) -> None:
    commitment = state["commitments"][payload["commitment_id"]]
    confirmation = {
        "party_id": payload["party_id"],
        "decision": payload["decision"],
        "confirmed_amount": payload.get("confirmed_amount"),
        "note": payload.get("note", ""),
        "confirmed_at": event["created_at"],
    }
    commitment["confirmations"].append(confirmation)
    if payload["decision"] == "confirmed":
        commitment["decision"] = "confirmed"
        confirmed_amount = commitment["amount"] if payload.get("confirmed_amount") is None else _money(payload["confirmed_amount"])
        commitment["confirmed_amount"] = confirmed_amount
    else:
        commitment["decision"] = "rejected"
        commitment["confirmed_amount"] = ZERO


def _call_capital(state, event, payload) -> None:
    state["calls"][payload["call_id"]] = {
        "call_id": payload["call_id"],
        "round_id": payload["round_id"],
        "window_id": payload["window_id"],
        "commitment_id": payload["commitment_id"],
        "party_id": payload.get("party_id"),
        "amount": _money(payload["amount"]),
        "purpose": payload.get("purpose", ""),
        "use_restriction_ids": list(payload.get("use_restriction_ids", [])),
        "idempotency_key": payload.get("idempotency_key"),
        "called_at": event["created_at"],
        "received_amount": ZERO,
    }


def _receive_capital(state, event, payload) -> None:
    call = state["calls"].get(payload["call_id"])
    amount = _money(payload["amount"])
    if call is not None:
        call["received_amount"] += amount
    state["receipts"].append(
        {
            "receipt_id": payload["receipt_id"],
            "call_id": payload["call_id"],
            "commitment_id": payload["commitment_id"],
            "amount": amount,
            "received_at": payload.get("received_at", event["created_at"]),
            "reference": payload.get("reference", ""),
            "recorded_at": event["created_at"],
        }
    )


def _declare_distribution(state, event, payload) -> None:
    allocations = {}
    for item in payload.get("allocations", []):
        allocations[item["commitment_id"]] = {
            "party_id": item["party_id"],
            "amount": _money(item["amount"]),
            "source_call_ids": list(item.get("source_call_ids", [])),
            "paid_amount": ZERO,
        }
    state["distributions"][payload["distribution_id"]] = {
        "distribution_id": payload["distribution_id"],
        "round_id": payload["round_id"],
        "window_id": payload.get("window_id"),
        "title": payload.get("title", ""),
        "category": payload.get("category", "return_of_capital"),
        "amount": _money(payload["amount"]),
        "payable_at": payload.get("payable_at"),
        "declared_at": event["created_at"],
        "allocations": allocations,
    }


def _pay_distribution(state, event, payload) -> None:
    distribution = state["distributions"][payload["distribution_id"]]
    allocation = distribution["allocations"][payload["commitment_id"]]
    allocation["paid_amount"] += _money(payload["amount"])
    state["payments"].append(
        {
            "distribution_id": payload["distribution_id"],
            "commitment_id": payload["commitment_id"],
            "party_id": payload["party_id"],
            "amount": _money(payload["amount"]),
            "paid_at": payload.get("paid_at", event["created_at"]),
            "reference": payload.get("reference", ""),
            "recorded_at": event["created_at"],
        }
    )


def _open_dispute(state, event, payload) -> None:
    state["disputes"].append(
        {
            "dispute_id": payload["dispute_id"],
            "scope": payload["scope"],
            "round_id": payload["round_id"],
            "commitment_id": payload.get("commitment_id"),
            "window_id": payload.get("window_id"),
            "call_id": payload.get("call_id"),
            "distribution_id": payload.get("distribution_id"),
            "amount": _money(payload.get("amount", 0)),
            "frozen_share_percent": _money(payload.get("frozen_share_percent", 100)),
            "reason": payload.get("reason", ""),
            "opened_by": event["actor_id"],
            "opened_at": event["created_at"],
            "resolved_at": None,
            "resolution": None,
            "note": None,
        }
    )


def _resolve_dispute(state, event, payload) -> None:
    for dispute in reversed(state["disputes"]):
        if dispute["dispute_id"] == payload["dispute_id"]:
            dispute["resolved_at"] = event["created_at"]
            dispute["resolution"] = payload["resolution"]
            dispute["note"] = payload.get("note", "")
            break


def _append_adjustment(state, event, payload) -> None:
    state["adjustments"].append(
        {
            "adjustment_id": payload["adjustment_id"],
            "scope": payload.get("scope", "commitment"),
            "round_id": payload["round_id"],
            "commitment_id": payload.get("commitment_id"),
            "call_id": payload.get("call_id"),
            "distribution_id": payload.get("distribution_id"),
            "kind": payload["kind"],
            "amount": _money(payload["amount"]),
            "reason": payload.get("reason", ""),
            "appended_at": event["created_at"],
            "appended_by": event["actor_id"],
        }
    )


def _close_round(state, event, payload) -> None:
    state["rounds"][payload["round_id"]]["closed_at"] = payload.get("closed_at", event["created_at"])


_HANDLERS = {
    "party.registered": _register_party,
    "round.version_published": _publish_round,
    "commitment.created": _create_commitment,
    "window.opened": _open_window,
    "condition.attached": _attach_condition,
    "condition.satisfied": _satisfy_condition,
    "follow_on.granted": _grant_follow_on,
    "follow_on.exercised": _exercise_follow_on,
    "use_restriction.imposed": _impose_restriction,
    "commitment.confirmed": _confirm_commitment,
    "capital.called": _call_capital,
    "capital.received": _receive_capital,
    "distribution.declared": _declare_distribution,
    "distribution.paid": _pay_distribution,
    "dispute.opened": _open_dispute,
    "dispute.resolved": _resolve_dispute,
    "adjustment.appended": _append_adjustment,
    "round.closed": _close_round,
}


# ---------------------------------------------------------------------------
# 派生查询
# ---------------------------------------------------------------------------


def active_disputes(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dispute for dispute in state["disputes"] if dispute["resolved_at"] is None]


def disputes_blocking_call(
    state: Mapping[str, Any], *, round_id: str, window_id: str, commitment_id: str
) -> list[dict[str, Any]]:
    """命中本次拟调用资金的未决争议；只返回真正相关的争议。"""

    result = []
    for dispute in active_disputes(state):
        if dispute["round_id"] != round_id:
            continue
        if dispute["scope"] == "commitment" and dispute["commitment_id"] == commitment_id:
            result.append(dispute)
        elif dispute["scope"] == "round":
            if dispute["commitment_id"] in (None, commitment_id):
                result.append(dispute)
        elif dispute["scope"] == "window" and dispute["window_id"] == window_id:
            if dispute["commitment_id"] in (None, commitment_id):
                result.append(dispute)
    return result


def disputes_blocking_receipt(
    state: Mapping[str, Any], *, round_id: str, call_id: str
) -> list[dict[str, Any]]:
    """命中某笔调用单后续实缴的未决争议。"""

    return [
        dispute
        for dispute in active_disputes(state)
        if dispute["round_id"] == round_id and dispute["scope"] == "call" and dispute["call_id"] == call_id
    ]


def disputed_call_ids(state: Mapping[str, Any], round_id: str) -> set[str]:
    """该轮中处于争议状态、不得作为返还来源的调用单。"""

    return {
        dispute["call_id"]
        for dispute in active_disputes(state)
        if dispute["round_id"] == round_id and dispute["scope"] == "call" and dispute["call_id"]
    }


def disputes_blocking_payment(
    state: Mapping[str, Any], *, round_id: str, distribution_id: str, commitment_id: str
) -> list[dict[str, Any]]:
    """命中某笔返还支付的未决争议；无争议份额照常支付。"""

    result = []
    for dispute in active_disputes(state):
        if dispute["round_id"] != round_id:
            continue
        if dispute["scope"] == "distribution" and dispute["distribution_id"] == distribution_id:
            if dispute["commitment_id"] in (None, commitment_id):
                result.append(dispute)
        elif dispute["scope"] == "commitment" and dispute["commitment_id"] == commitment_id:
            result.append(dispute)
    return result


def _commitment_adjustment(commitment: Mapping[str, Any], adjustments: Sequence[Mapping[str, Any]]) -> Decimal:
    delta = ZERO
    for adjustment in adjustments:
        if adjustment["scope"] != "commitment" or adjustment["commitment_id"] != commitment["commitment_id"]:
            continue
        if adjustment["kind"] in ("write_off", "cancelled"):
            delta -= adjustment["amount"]
        elif adjustment["kind"] == "reinstated":
            delta += adjustment["amount"]
    return delta


def _commitment_calls(state: Mapping[str, Any], commitment_id: str) -> list[dict[str, Any]]:
    return [call for call in state["calls"].values() if call["commitment_id"] == commitment_id]


def _frozen_amount(state: Mapping[str, Any], commitment: Mapping[str, Any]) -> Decimal:
    """精确冻结到该承诺的未决争议金额（不含整轮/整窗的共同冻结）。"""

    total = ZERO
    for dispute in active_disputes(state):
        if dispute["round_id"] != commitment["round_id"]:
            continue
        # 行级冻结只精确计入指向该承诺的承诺级争议；窗口/整轮共同争议即使金额
        # 未定到具体承诺也仍会阻断调用，其冻结额在轮级 open_disputes 中汇总，
        # 避免同一金额在多条承诺行上被重复计算。调用单/分配争议针对已发生金额，
        # 不在可调用余额中重复冻结。
        if dispute["scope"] != "commitment":
            continue
        if dispute["commitment_id"] == commitment["commitment_id"]:
            total += quantize_money(dispute["amount"] * dispute["frozen_share_percent"] / Decimal("100"))
    return total


def commitment_line(state: Mapping[str, Any], commitment_id: str) -> dict[str, Any]:
    commitment = state["commitments"][commitment_id]
    calls = _commitment_calls(state, commitment_id)
    called = quantize_money(sum((call["amount"] for call in calls), ZERO))
    received = quantize_money(sum((call["received_amount"] for call in calls), ZERO))
    returned = quantize_money(
        sum(
            (
                allocation["paid_amount"]
                for distribution in state["distributions"].values()
                if distribution["round_id"] == commitment["round_id"]
                for cid, allocation in distribution["allocations"].items()
                if cid == commitment_id
            ),
            ZERO,
        )
    )
    adjustment = quantize_money(_commitment_adjustment(commitment, state["adjustments"]))
    basis = quantize_money(commitment["confirmed_amount"] + adjustment)
    frozen = min(_frozen_amount(state, commitment), max(ZERO, basis - called))
    return {
        "commitment_id": commitment_id,
        "party_id": commitment["party_id"],
        "status": commitment["decision"],
        "committed_amount": money_text(commitment["confirmed_amount"]),
        "adjustment_amount": money_text(adjustment),
        "effective_commitment": money_text(basis),
        "called_amount": money_text(called),
        "received_amount": money_text(received),
        "outstanding_called": money_text(quantize_money(called - received)),
        "returned_amount": money_text(returned),
        "net_contributed": money_text(quantize_money(received - returned)),
        "frozen_amount": money_text(quantize_money(frozen)),
        "available_to_call": money_text(quantize_money(max(ZERO, basis - called - frozen))),
        "currency": commitment["currency"],
    }


def round_statement(state: Mapping[str, Any], round_id: str) -> dict[str, Any]:
    if round_id not in state["rounds"]:
        raise KeyError(round_id)
    commitments = [
        commitment_line(state, commitment_id)
        for commitment_id in sorted(
            cid for cid, item in state["commitments"].items() if item["round_id"] == round_id
        )
    ]
    totals = {
        key: money_text(
            quantize_money(sum((Decimal(line[key]) for line in commitments), ZERO))
        )
        for key in (
            "committed_amount",
            "adjustment_amount",
            "effective_commitment",
            "called_amount",
            "received_amount",
            "outstanding_called",
            "returned_amount",
            "net_contributed",
            "frozen_amount",
            "available_to_call",
        )
    }
    open_disputes = [
        {
            "dispute_id": dispute["dispute_id"],
            "scope": dispute["scope"],
            "commitment_id": dispute["commitment_id"],
            "window_id": dispute["window_id"],
            "call_id": dispute["call_id"],
            "distribution_id": dispute["distribution_id"],
            "frozen_amount": money_text(
                quantize_money(dispute["amount"] * dispute["frozen_share_percent"] / Decimal("100"))
            ),
            "reason": dispute["reason"],
        }
        for dispute in active_disputes(state)
        if dispute["round_id"] == round_id
    ]
    round_record = state["rounds"][round_id]
    return {
        "round_id": round_id,
        "project_id": round_record["project_id"],
        "current_version": round_record["versions"][-1]["version"] if round_record["versions"] else None,
        "closed": round_record["closed_at"] is not None,
        "commitments": commitments,
        "totals": totals,
        "open_disputes": open_disputes,
        "events_applied": state["events_applied"],
        "as_of_event_at": state["last_event_at"],
    }


def call_eligibility(
    state: Mapping[str, Any], *, commitment_id: str, window_id: str, amount: Decimal, as_of: str
) -> dict[str, Any]:
    """解释一笔拟调用资金为何可执行或被哪些条件/争议阻挡。"""

    reasons: list[dict[str, str]] = []
    commitment = state["commitments"].get(commitment_id)
    window = state["windows"].get(window_id)
    eligible = True

    def block(code: str, message: str) -> None:
        nonlocal eligible
        eligible = False
        reasons.append({"code": code, "message": message})

    if commitment is None:
        block("commitment_missing", "承诺不存在")
        return {"eligible": False, "reasons": reasons}
    if window is None:
        block("window_missing", "出资窗口不存在")
        return {"eligible": False, "reasons": reasons}
    if commitment["round_id"] != window["round_id"]:
        block("round_mismatch", "承诺与出资窗口不属于同一轮协议")
    if commitment["decision"] != "confirmed":
        block("commitment_not_confirmed", f"参与方尚未确认承诺（当前状态 {commitment['decision']}）")
    round_record = state["rounds"].get(window["round_id"])
    if round_record is not None and round_record["closed_at"] is not None:
        block("round_closed", "该轮协议已关闭，不得再调用资金")
    if as_of < window["opens_at"]:
        block("window_not_open", f"出资窗口尚未开启（{window['opens_at']}）")
    if as_of > window["closes_at"]:
        block("window_closed", f"出资窗口已截止（{window['closes_at']}）")

    def unsatisfied(condition: Mapping[str, Any]) -> bool:
        return condition["blocking"] and not condition["satisfied"]

    for condition_id in window["condition_ids"]:
        condition = state["conditions"].get(condition_id)
        if condition is not None and unsatisfied(condition):
            block("condition_pending", f"前置条件尚未满足：{condition['title'] or condition_id}")
    for condition in state["conditions"].values():
        if condition["round_id"] != window["round_id"]:
            continue
        if condition["scope"] == "round" and unsatisfied(condition):
            block("condition_pending", f"轮级前置条件尚未满足：{condition['title'] or condition['condition_id']}")
        if condition["scope"] == "commitment" and condition["commitment_id"] == commitment_id and unsatisfied(condition):
            block("condition_pending", f"承诺前置条件尚未满足：{condition['title'] or condition['condition_id']}")

    blocking_disputes = disputes_blocking_call(
        state, round_id=window["round_id"], window_id=window_id, commitment_id=commitment_id
    )
    for dispute in blocking_disputes:
        block("dispute_frozen", f"争议 {dispute['dispute_id']} 冻结了相关承诺或金额：{dispute['reason']}")

    line = commitment_line(state, commitment_id)
    available = Decimal(line["available_to_call"])
    if amount > available:
        block(
            "insufficient_available",
            f"可调用余额 {money_text(available)} 不足以覆盖本次调用 {money_text(quantize_money(amount))}",
        )

    missing_restrictions = [
        restriction_id
        for restriction_id in window["use_restriction_ids"]
        if restriction_id not in state["restrictions"]
    ]
    if missing_restrictions:
        block("restriction_unknown", f"窗口引用了未登记的用途限制：{missing_restrictions}")

    return {
        "eligible": eligible,
        "reasons": reasons,
        "available_to_call": line["available_to_call"],
        "frozen_amount": line["frozen_amount"],
    }


def explain_receipt(state: Mapping[str, Any], receipt_id: str) -> dict[str, Any]:
    """追溯一笔实缴投入：调用单、窗口条件、用途限制与承诺确认。"""

    receipt = next((item for item in state["receipts"] if item["receipt_id"] == receipt_id), None)
    if receipt is None:
        raise KeyError(receipt_id)
    call = state["calls"].get(receipt["call_id"])
    if call is None:
        raise KeyError(receipt["call_id"])
    commitment = state["commitments"][call["commitment_id"]]
    window = state["windows"].get(call["window_id"], {})
    conditions = [
        {
            "condition_id": condition_id,
            "title": state["conditions"].get(condition_id, {}).get("title", ""),
            "satisfied": state["conditions"].get(condition_id, {}).get("satisfied", False),
            "satisfied_at": state["conditions"].get(condition_id, {}).get("satisfied_at"),
        }
        for condition_id in window.get("condition_ids", [])
    ]
    return {
        "cashflow": "contribution",
        "receipt_id": receipt_id,
        "amount": money_text(quantize_money(receipt["amount"])),
        "received_at": receipt["received_at"],
        "reference": receipt["reference"],
        "basis": {
            "call_id": call["call_id"],
            "called_at": call["called_at"],
            "window_id": call["window_id"],
            "window_opens_at": window.get("opens_at"),
            "window_closes_at": window.get("closes_at"),
            "purpose": call["purpose"],
            "use_restrictions": [
                state["restrictions"].get(rid, {"restriction_id": rid}) for rid in call["use_restriction_ids"]
            ],
            "conditions": conditions,
            "commitment_id": commitment["commitment_id"],
            "party_id": commitment["party_id"],
            "commitment_decision": commitment["decision"],
            "confirmations": commitment["confirmations"],
        },
    }


def explain_payment(state: Mapping[str, Any], distribution_id: str, commitment_id: str) -> dict[str, Any]:
    """追溯一笔返还：分配决议、该参与方的份额与实缴来源。"""

    distribution = state["distributions"].get(distribution_id)
    if distribution is None or commitment_id not in distribution["allocations"]:
        raise KeyError((distribution_id, commitment_id))
    allocation = distribution["allocations"][commitment_id]
    commitment = state["commitments"][commitment_id]
    source_calls = [
        {
            "call_id": call_id,
            "amount": money_text(state["calls"][call_id]["amount"]),
            "received_amount": money_text(state["calls"][call_id]["received_amount"]),
        }
        for call_id in allocation["source_call_ids"]
        if call_id in state["calls"]
    ]
    return {
        "cashflow": "return",
        "distribution_id": distribution_id,
        "commitment_id": commitment_id,
        "party_id": allocation["party_id"],
        "declared_amount": money_text(quantize_money(allocation["amount"])),
        "paid_amount": money_text(quantize_money(allocation["paid_amount"])),
        "category": distribution["category"],
        "payable_at": distribution["payable_at"],
        "paid_at": next(
            (
                payment["paid_at"]
                for payment in state["payments"]
                if payment["distribution_id"] == distribution_id and payment["commitment_id"] == commitment_id
            ),
            None,
        ),
        "basis": {
            "declared_at": distribution["declared_at"],
            "title": distribution["title"],
            "source_calls": source_calls,
            "commitment_committed": money_text(commitment["confirmed_amount"]),
            "commitment_received": money_text(
                quantize_money(sum((c["received_amount"] for c in _commitment_calls(state, commitment_id)), ZERO))
            ),
        },
    }
