"""联合资本承诺与结算的领域输入契约。

一个协议版本（agreement revision）由参与方承诺组成，承诺上可以挂：
- 出资窗口（funding windows）：可被调用的时间区间；
- 先决条件（conditions）：未满足时对应金额不得调用；
- 跟投权（follow-on rights）：后续轮次的按比例参与权；
- 用途限制（use restrictions）：资金只能用于指定用途类目；
- 退出限制（exit restrictions）：转让锁定期等份额处置约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PARTY_KINDS = {"fof", "market_fund", "corporate", "carry_vehicle"}
WINDOW_TYPES = {"tranche", "milestone", "evergreen"}
CONDITION_TYPES = {"milestone", "regulatory", "clinical", "commercial", "custom"}
CONDITION_STATUSES = {"pending", "satisfied", "waived", "failed"}
RESTRICTION_TYPES = {"use", "exit_lock", "transfer_notice"}
CASHFLOW_DIRECTIONS = {"contribution", "return"}
RETURN_KINDS = {"distribution", "refund", "milestone_return"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field_name: str, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return required_text(value, field_name, maximum)


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def decimal_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field_name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field_name} 不能大于 {maximum}")
    return result


def date_text(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc


def optional_date(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return date_text(value, field_name)


def text_list(value: object, field_name: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field_name} 必须是非空数组")
    result: list[str] = []
    for index, item in enumerate(value):
        text = required_text(item, f"{field_name}[{index}]", 64)
        if text not in result:
            result.append(text)
    if len(result) < minimum:
        raise ValidationFailed(f"{field_name} 至少包含 {minimum} 个不重复条目")
    return result


@dataclass(frozen=True, slots=True)
class FundingWindow:
    window_id: str
    window_type: str
    opens_on: str
    closes_on: str | None
    amount: Decimal
    order_index: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "FundingWindow":
        window_type = required_text(raw.get("window_type"), "window_type", 24)
        if window_type not in WINDOW_TYPES:
            raise ValidationFailed("window_type 必须是 tranche、milestone 或 evergreen")
        opens_on = date_text(raw.get("opens_on"), "opens_on")
        closes_on = optional_date(raw.get("closes_on"), "closes_on")
        if closes_on is not None and closes_on < opens_on:
            raise ValidationFailed("closes_on 不能早于 opens_on")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            window_type=window_type,
            opens_on=opens_on,
            closes_on=closes_on,
            amount=decimal_value(raw.get("amount"), "amount", minimum=Decimal("0.01")),
            order_index=int(index),
        )


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    condition_id: str
    condition_type: str
    label: str
    gate_amount: Decimal | None
    blocking_window_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConditionSpec":
        condition_type = required_text(raw.get("condition_type"), "condition_type", 24)
        if condition_type not in CONDITION_TYPES:
            raise ValidationFailed("condition_type 不是受支持的条件类型")
        gate_amount = raw.get("gate_amount")
        return cls(
            condition_id=identifier(raw.get("condition_id"), "condition_id"),
            condition_type=condition_type,
            label=required_text(raw.get("label"), "label"),
            gate_amount=None if gate_amount is None else decimal_value(
                gate_amount, "gate_amount", minimum=Decimal("0.01")
            ),
            blocking_window_ids=tuple(text_list(raw.get("blocking_window_ids"), "blocking_window_ids")),
        )


@dataclass(frozen=True, slots=True)
class FollowOnRight:
    right_id: str
    future_round_code: str
    pro_rata_percent: Decimal
    exercise_window_days: int | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FollowOnRight":
        days = raw.get("exercise_window_days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days <= 0):
            raise ValidationFailed("exercise_window_days 必须是正整数")
        return cls(
            right_id=identifier(raw.get("right_id"), "right_id"),
            future_round_code=identifier(raw.get("future_round_code"), "future_round_code"),
            pro_rata_percent=decimal_value(
                raw.get("pro_rata_percent"), "pro_rata_percent",
                minimum=Decimal("0.01"), maximum=Decimal("100"),
            ),
            exercise_window_days=days,
        )


@dataclass(frozen=True, slots=True)
class UseRestriction:
    restriction_id: str
    restriction_type: str
    allowed_categories: tuple[str, ...]
    notice_required: bool
    note: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UseRestriction":
        restriction_type = required_text(raw.get("restriction_type"), "restriction_type", 32)
        if restriction_type not in RESTRICTION_TYPES:
            raise ValidationFailed("restriction_type 不是受支持的限制类型")
        categories: tuple[str, ...] = ()
        if restriction_type == "use":
            categories = tuple(text_list(raw.get("allowed_categories"), "allowed_categories"))
        return cls(
            restriction_id=identifier(raw.get("restriction_id"), "restriction_id"),
            restriction_type=restriction_type,
            allowed_categories=categories,
            notice_required=bool(raw.get("notice_required", False)),
            note=optional_text(raw.get("note"), "note", 512),
        )


@dataclass(frozen=True, slots=True)
class CommitmentSpec:
    party_id: str
    party_kind: str
    committed_amount: Decimal
    windows: tuple[FundingWindow, ...]
    conditions: tuple[ConditionSpec, ...]
    follow_on_rights: tuple[FollowOnRight, ...]
    restrictions: tuple[UseRestriction, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentSpec":
        party_kind = required_text(raw.get("party_kind"), "party_kind", 24)
        if party_kind not in PARTY_KINDS:
            raise ValidationFailed("party_kind 必须是 fof、market_fund、corporate 或 carry_vehicle")
        windows_raw = raw.get("funding_windows")
        if not isinstance(windows_raw, list) or not windows_raw:
            raise ValidationFailed("funding_windows 必须是非空数组")
        windows = tuple(
            FundingWindow.from_dict(item, index)
            for index, item in enumerate(windows_raw)
        )
        window_total = sum((item.amount for item in windows), Decimal("0"))
        committed_amount = decimal_value(
            raw.get("committed_amount"), "committed_amount", minimum=Decimal("0.01")
        )
        if window_total != committed_amount:
            raise ValidationFailed(
                f"出资窗口金额合计 {window_total} 必须等于承诺金额 {committed_amount}"
            )
        window_ids = [item.window_id for item in windows]
        if len(set(window_ids)) != len(window_ids):
            raise ValidationFailed("出资窗口编号不能重复")
        conditions_raw = raw.get("conditions", [])
        if not isinstance(conditions_raw, list):
            raise ValidationFailed("conditions 必须是数组")
        conditions = tuple(ConditionSpec.from_dict(item) for item in conditions_raw)
        blocked = {window_id for item in conditions for window_id in item.blocking_window_ids}
        unknown = blocked - set(window_ids)
        if unknown:
            raise ValidationFailed(f"条件引用了不存在的出资窗口: {sorted(unknown)}")
        rights_raw = raw.get("follow_on_rights", [])
        if not isinstance(rights_raw, list):
            raise ValidationFailed("follow_on_rights 必须是数组")
        restrictions_raw = raw.get("restrictions", [])
        if not isinstance(restrictions_raw, list):
            raise ValidationFailed("restrictions 必须是数组")
        return cls(
            party_id=identifier(raw.get("party_id"), "party_id"),
            party_kind=party_kind,
            committed_amount=committed_amount,
            windows=windows,
            conditions=conditions,
            follow_on_rights=tuple(FollowOnRight.from_dict(item) for item in rights_raw),
            restrictions=tuple(UseRestriction.from_dict(item) for item in restrictions_raw),
        )


@dataclass(frozen=True, slots=True)
class AgreementRevision:
    project_id: str
    round_code: str
    revision: int
    basis_revision: int | None
    reason: str | None
    commitments: tuple[CommitmentSpec, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], revision: int, basis_revision: int | None) -> "AgreementRevision":
        revision_value = raw.get("revision", revision)
        if isinstance(revision_value, bool) or not isinstance(revision_value, int) or revision_value <= 0:
            raise ValidationFailed("revision 必须是正整数")
        if revision_value != revision:
            raise ValidationFailed(f"协议版本号必须为 {revision}")
        commitments_raw = raw.get("commitments")
        if not isinstance(commitments_raw, list) or not commitments_raw:
            raise ValidationFailed("commitments 必须是非空数组")
        commitments = tuple(CommitmentSpec.from_dict(item) for item in commitments_raw)
        party_ids = [item.party_id for item in commitments]
        if len(set(party_ids)) != len(party_ids):
            raise ValidationFailed("同一协议版本内参与方不能重复")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            round_code=identifier(raw.get("round_code"), "round_code"),
            revision=revision_value,
            basis_revision=basis_revision,
            reason=optional_text(raw.get("reason"), "reason", 512),
            commitments=commitments,
        )


@dataclass(frozen=True, slots=True)
class CashEventInput:
    event_id: str
    direction: str
    party_id: str
    window_id: str | None
    return_kind: str | None
    amount: Decimal
    occurred_on: str
    use_category: str | None
    idempotency_key: str
    note: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CashEventInput":
        direction = required_text(raw.get("direction"), "direction", 16)
        if direction not in CASHFLOW_DIRECTIONS:
            raise ValidationFailed("direction 必须是 contribution 或 return")
        window_value = raw.get("window_id")
        window_id = None if window_value is None else identifier(window_value, "window_id")
        if direction == "contribution" and window_id is None:
            raise ValidationFailed("出资现金流必须指定 window_id")
        return_kind = raw.get("return_kind")
        if direction == "return":
            return_kind = required_text(return_kind, "return_kind", 32)
            if return_kind not in RETURN_KINDS:
                raise ValidationFailed("return_kind 必须是 distribution、refund 或 milestone_return")
        elif return_kind is not None:
            raise ValidationFailed("出资现金流不能指定 return_kind")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            direction=direction,
            party_id=identifier(raw.get("party_id"), "party_id"),
            window_id=window_id,
            return_kind=return_kind,
            amount=decimal_value(raw.get("amount"), "amount", minimum=Decimal("0.01")),
            occurred_on=date_text(raw.get("occurred_on"), "occurred_on"),
            use_category=optional_text(raw.get("use_category"), "use_category", 64),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            note=optional_text(raw.get("note"), "note", 512),
        )


@dataclass(frozen=True, slots=True)
class DisputeSpec:
    dispute_id: str
    party_id: str
    window_id: str | None
    amount: Decimal
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DisputeSpec":
        window_value = raw.get("window_id")
        return cls(
            dispute_id=identifier(raw.get("dispute_id"), "dispute_id"),
            party_id=identifier(raw.get("party_id"), "party_id"),
            window_id=None if window_value is None else identifier(window_value, "window_id"),
            amount=decimal_value(raw.get("amount"), "amount", minimum=Decimal("0.01")),
            reason=required_text(raw.get("reason"), "reason", 512),
        )
