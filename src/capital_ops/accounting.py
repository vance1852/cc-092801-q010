"""确定性的承诺可调用性、争议冻结、现金流与周期核算。

所有函数都是纯函数：服务层从 SQLite 取出行（金额以字符串保存），
由这里完成 Decimal 计算，周期核算随时可以从有效事件（state='booked'）重新计算。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
CENT = Decimal("0.01")
UNSATISFIED_STATUSES = {"pending", "failed"}


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def money(value: object) -> Decimal:
    return quantize_money(Decimal(str(value)))


@dataclass(frozen=True, slots=True)
class WindowBlocker:
    condition_code: str
    status: str
    gate_amount: Decimal | None


@dataclass(frozen=True, slots=True)
class WindowStanding:
    window_id: int
    window_code: str
    window_type: str
    amount: Decimal
    opens_on: str
    closes_on: str | None
    open_now: bool
    gated_amount: Decimal
    gate_reasons: tuple[str, ...]
    contributed: Decimal
    window_frozen: Decimal

    @property
    def callable_cap(self) -> Decimal:
        """窗口当前仍可被调用的上限（不含承诺级冻结）。"""
        if not self.open_now:
            return ZERO
        remaining = self.amount - self.gated_amount - self.contributed - self.window_frozen
        return quantize_money(max(ZERO, remaining))

    def as_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "window_code": self.window_code,
            "window_type": self.window_type,
            "amount": decimal_text(self.amount),
            "opens_on": self.opens_on,
            "closes_on": self.closes_on,
            "open_now": self.open_now,
            "gated_amount": decimal_text(self.gated_amount),
            "gate_reasons": list(self.gate_reasons),
            "contributed": decimal_text(self.contributed),
            "window_frozen": decimal_text(self.window_frozen),
            "callable_cap": decimal_text(self.callable_cap),
        }


def evaluate_windows(
    windows: Sequence[Mapping[str, object]],
    blockers: Mapping[int, Sequence[WindowBlocker]],
    contributed_by_window: Mapping[int, Decimal],
    frozen_by_window: Mapping[int, Decimal],
    as_of: str,
) -> list[WindowStanding]:
    """逐窗口计算开放状态、条件门控金额、已出资与窗口级冻结。"""
    result: list[WindowStanding] = []
    for row in sorted(windows, key=lambda item: int(item["order_index"])):
        window_id = int(row["funding_window_id"])
        amount = money(row["amount"])
        opens_on = str(row["opens_on"])
        closes_on = row["closes_on"]
        closes_text = None if closes_on is None else str(closes_on)
        open_now = opens_on <= as_of and (closes_text is None or closes_text >= as_of)
        gate_total = ZERO
        reasons: list[str] = []
        for blocker in blockers.get(window_id, ()):
            portion = amount if blocker.gate_amount is None else blocker.gate_amount
            gate_total += portion
            reasons.append(f"condition:{blocker.condition_code}:{blocker.status}")
        gated = quantize_money(min(amount, max(ZERO, gate_total)))
        result.append(
            WindowStanding(
                window_id=window_id,
                window_code=str(row["window_code"]),
                window_type=str(row["window_type"]),
                amount=amount,
                opens_on=opens_on,
                closes_on=closes_text,
                open_now=open_now,
                gated_amount=gated,
                gate_reasons=tuple(reasons),
                contributed=quantize_money(contributed_by_window.get(window_id, ZERO)),
                window_frozen=quantize_money(frozen_by_window.get(window_id, ZERO)),
            )
        )
    return result


def allocate_return(
    window_net: Sequence[tuple[str, Decimal]],
    amount: Decimal,
) -> list[dict[str, object]]:
    """按出资窗口先后顺序，从各窗口净投入中分配一笔返还，作为返还依据。"""
    remaining = quantize_money(amount)
    allocation: list[dict[str, object]] = []
    for window_code, net in window_net:
        available = quantize_money(net)
        if available <= ZERO or remaining <= ZERO:
            continue
        taken = quantize_money(min(available, remaining))
        allocation.append({"window_code": window_code, "amount": decimal_text(taken)})
        remaining -= taken
    if remaining > ZERO:
        raise ValueError("返还金额超过参与方净投入")
    return allocation


def period_totals(events: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """从有效现金流事件重新计算周期合计与逐笔余额。"""
    contributions = ZERO
    returns = ZERO
    rows: list[dict[str, object]] = []
    balance = ZERO
    for event in sorted(
        events,
        key=lambda item: (str(item["occurred_on"]), str(item["event_id"])),
    ):
        amount = money(event["amount"])
        if event["direction"] == "contribution":
            contributions += amount
            balance += amount
        else:
            returns += amount
            balance -= amount
        rows.append({
            "event_id": event["event_id"],
            "direction": event["direction"],
            "return_kind": event["return_kind"],
            "party_id": event["party_id"],
            "amount": decimal_text(quantize_money(amount)),
            "occurred_on": event["occurred_on"],
            "running_net": decimal_text(quantize_money(balance)),
        })
    return {
        "contributions": decimal_text(quantize_money(contributions)),
        "returns": decimal_text(quantize_money(returns)),
        "net_change": decimal_text(quantize_money(contributions - returns)),
        "period_end_net": decimal_text(quantize_money(balance)),
        "event_count": len(rows),
        "events": rows,
    }
