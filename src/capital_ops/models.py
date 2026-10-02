"""联合资本承诺与结算领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

PARTY_KINDS = {"fund_of_funds", "market_fund", "industrial", "gp", "spv"}
CONDITION_SCOPES = {"round", "window", "commitment"}
DISPUTE_SCOPES = {"commitment", "window", "call", "distribution", "round"}
DISPUTE_RESOLUTIONS = {"released", "upheld", "split"}
ADJUSTMENT_KINDS = {"write_off", "reinstated", "cancelled", "reallocation"}
DISTRIBUTION_CATEGORIES = {"return_of_capital", "dividend", "proceeds", "fee_refund"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return identifier(value, field)


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # InvalidOperation/ValueError/TypeError
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def amount(value: object, field: str, *, minimum: Decimal = Decimal("0.01")) -> Decimal:
    return decimal_value(value, field, minimum=minimum)


def iso_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        from .clock import utc_text

        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def optional_iso_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return iso_text(value, field)


def choice(value: object, field: str, allowed: set[str]) -> str:
    result = required_text(value, field, 32)
    if result not in allowed:
        raise ValidationFailed(f"{field} 必须是 {sorted(allowed)} 之一")
    return result


def identifier_list(value: object, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationFailed(f"{field} 必须是数组")
    result = [identifier(item, f"{field}[]") for item in value]
    if len(set(result)) != len(result):
        raise ValidationFailed(f"{field} 不能重复")
    return result


@dataclass(frozen=True, slots=True)
class PartyInput:
    party_id: str
    display_name: str
    kind: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PartyInput":
        return cls(
            party_id=identifier(raw.get("party_id"), "party_id"),
            display_name=required_text(raw.get("display_name"), "display_name"),
            kind=choice(raw.get("kind", "lp"), "kind", PARTY_KINDS),
        )


@dataclass(frozen=True, slots=True)
class RoundVersionInput:
    round_id: str
    version: int
    title: str
    effective_at: str
    note: str
    supersedes_version: int | None
    project_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoundVersionInput":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationFailed("version 必须是正整数")
        supersedes = raw.get("supersedes_version")
        if supersedes is not None and (isinstance(supersedes, bool) or not isinstance(supersedes, int) or supersedes <= 0):
            raise ValidationFailed("supersedes_version 必须是正整数或空")
        return cls(
            round_id=identifier(raw.get("round_id"), "round_id"),
            version=version,
            title=required_text(raw.get("title"), "title"),
            effective_at=iso_text(raw.get("effective_at"), "effective_at"),
            note=required_text(raw.get("note", ""), "note", 1024) if raw.get("note") else "",
            supersedes_version=supersedes,
            project_id=required_text(raw.get("project_id"), "project_id"),
        )


@dataclass(frozen=True, slots=True)
class CommitmentInput:
    commitment_id: str
    round_id: str
    round_version: int
    party_id: str
    amount_value: Decimal
    currency: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentInput":
        currency = required_text(raw.get("currency", "CNY"), "currency", 8)
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            round_version=_version(raw.get("round_version")),
            party_id=identifier(raw.get("party_id"), "party_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            currency=currency.upper(),
            note=required_text(raw.get("note", ""), "note", 1024) if raw.get("note") else "",
        )


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed("round_version 必须是正整数")
    return value


@dataclass(frozen=True, slots=True)
class WindowInput:
    window_id: str
    round_id: str
    title: str
    opens_at: str
    closes_at: str
    conditions: list[str]
    use_restrictions: list[str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WindowInput":
        opens_at = iso_text(raw.get("opens_at"), "opens_at")
        closes_at = iso_text(raw.get("closes_at"), "closes_at")
        if closes_at <= opens_at:
            raise ValidationFailed("closes_at 必须晚于 opens_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            title=required_text(raw.get("title"), "title"),
            opens_at=opens_at,
            closes_at=closes_at,
            conditions=identifier_list(raw.get("conditions", []), "conditions"),
            use_restrictions=identifier_list(raw.get("use_restrictions", []), "use_restrictions"),
        )


@dataclass(frozen=True, slots=True)
class ConditionInput:
    condition_id: str
    round_id: str
    scope: str
    title: str
    blocking: bool
    window_id: str | None
    commitment_id: str | None
    depends_on: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConditionInput":
        scope = choice(raw.get("scope", "window"), "scope", CONDITION_SCOPES)
        window_id = optional_identifier(raw.get("window_id"), "window_id")
        commitment_id = optional_identifier(raw.get("commitment_id"), "commitment_id")
        if scope == "window" and window_id is None:
            raise ValidationFailed("窗口级条件必须提供 window_id")
        if scope == "commitment" and commitment_id is None:
            raise ValidationFailed("承诺级条件必须提供 commitment_id")
        blocking = raw.get("blocking", True)
        if not isinstance(blocking, bool):
            raise ValidationFailed("blocking 必须是布尔值")
        return cls(
            condition_id=identifier(raw.get("condition_id"), "condition_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            scope=scope,
            title=required_text(raw.get("title"), "title"),
            blocking=blocking,
            window_id=window_id,
            commitment_id=commitment_id,
            depends_on=optional_identifier(raw.get("depends_on"), "depends_on"),
        )


@dataclass(frozen=True, slots=True)
class FollowOnGrant:
    follow_on_id: str
    round_id: str
    holder_party_id: str
    grant_commitment_id: str | None
    share_percent: Decimal
    max_amount_value: Decimal | None
    window_id: str | None
    expires_at: str | None
    title: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FollowOnGrant":
        return cls(
            follow_on_id=identifier(raw.get("follow_on_id"), "follow_on_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            holder_party_id=identifier(raw.get("holder_party_id"), "holder_party_id"),
            grant_commitment_id=optional_identifier(raw.get("grant_commitment_id"), "grant_commitment_id"),
            share_percent=decimal_value(raw.get("share_percent", 0), "share_percent", minimum=Decimal("0"), maximum=Decimal("100")),
            max_amount_value=(None if raw.get("max_amount") is None else amount(raw.get("max_amount"), "max_amount")),
            window_id=optional_identifier(raw.get("window_id"), "window_id"),
            expires_at=optional_iso_text(raw.get("expires_at"), "expires_at"),
            title=required_text(raw.get("title", ""), "title") if raw.get("title") else "",
        )


@dataclass(frozen=True, slots=True)
class FollowOnExercise:
    follow_on_id: str
    amount_value: Decimal
    new_commitment_id: str
    round_version: int
    into_window_id: str | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FollowOnExercise":
        return cls(
            follow_on_id=identifier(raw.get("follow_on_id"), "follow_on_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            new_commitment_id=identifier(raw.get("new_commitment_id"), "new_commitment_id"),
            round_version=_version(raw.get("round_version")),
            into_window_id=optional_identifier(raw.get("into_window_id"), "into_window_id"),
            note=required_text(raw.get("note", ""), "note", 1024) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class RestrictionInput:
    restriction_id: str
    round_id: str
    title: str
    purpose: str
    window_id: str | None
    commitment_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RestrictionInput":
        return cls(
            restriction_id=identifier(raw.get("restriction_id"), "restriction_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            title=required_text(raw.get("title"), "title"),
            purpose=required_text(raw.get("purpose"), "purpose", 64),
            window_id=optional_identifier(raw.get("window_id"), "window_id"),
            commitment_id=optional_identifier(raw.get("commitment_id"), "commitment_id"),
        )


@dataclass(frozen=True, slots=True)
class ConfirmationInput:
    commitment_id: str
    decision: str
    confirmed_amount_value: Decimal | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConfirmationInput":
        decision = choice(raw.get("decision"), "decision", {"confirmed", "rejected"})
        confirmed = raw.get("confirmed_amount")
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            decision=decision,
            confirmed_amount_value=(
                None if confirmed is None else amount(confirmed, "confirmed_amount", minimum=Decimal("0"))
            ),
            note=required_text(raw.get("note", ""), "note", 1024) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class CapitalCallInput:
    call_id: str
    window_id: str
    commitment_id: str
    amount_value: Decimal
    purpose: str
    use_restrictions: list[str]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapitalCallInput":
        return cls(
            call_id=identifier(raw.get("call_id"), "call_id"),
            window_id=identifier(raw.get("window_id"), "window_id"),
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            purpose=required_text(raw.get("purpose"), "purpose", 64),
            use_restrictions=identifier_list(raw.get("use_restrictions", []), "use_restrictions"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptInput:
    receipt_id: str
    call_id: str
    amount_value: Decimal
    received_at: str
    reference: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptInput":
        return cls(
            receipt_id=identifier(raw.get("receipt_id"), "receipt_id"),
            call_id=identifier(raw.get("call_id"), "call_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            received_at=iso_text(raw.get("received_at"), "received_at"),
            reference=required_text(raw.get("reference", ""), "reference", 128) if raw.get("reference") else "",
        )


@dataclass(frozen=True, slots=True)
class DistributionAllocation:
    commitment_id: str
    party_id: str
    amount_value: Decimal
    source_call_ids: list[str]


@dataclass(frozen=True, slots=True)
class DistributionInput:
    distribution_id: str
    round_id: str
    title: str
    category: str
    amount_value: Decimal
    payable_at: str
    window_id: str | None
    allocations: tuple[DistributionAllocation, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DistributionInput":
        allocations_raw = raw.get("allocations")
        if not isinstance(allocations_raw, Sequence) or isinstance(allocations_raw, (str, bytes)) or not allocations_raw:
            raise ValidationFailed("allocations 必须是非空数组")
        allocations = tuple(_allocation(item, index) for index, item in enumerate(allocations_raw))
        return cls(
            distribution_id=identifier(raw.get("distribution_id"), "distribution_id"),
            round_id=identifier(raw.get("round_id"), "round_id"),
            title=required_text(raw.get("title"), "title"),
            category=choice(raw.get("category", "return_of_capital"), "category", DISTRIBUTION_CATEGORIES),
            amount_value=amount(raw.get("amount"), "amount"),
            payable_at=iso_text(raw.get("payable_at"), "payable_at"),
            window_id=optional_identifier(raw.get("window_id"), "window_id"),
            allocations=allocations,
        )


def _allocation(raw: object, index: int) -> DistributionAllocation:
    if not isinstance(raw, Mapping):
        raise ValidationFailed(f"allocations[{index}] 必须是对象")
    return DistributionAllocation(
        commitment_id=identifier(raw.get("commitment_id"), f"allocations[{index}].commitment_id"),
        party_id=identifier(raw.get("party_id"), f"allocations[{index}].party_id"),
        amount_value=amount(raw.get("amount"), f"allocations[{index}].amount"),
        source_call_ids=identifier_list(raw.get("source_call_ids", []), f"allocations[{index}].source_call_ids"),
    )


@dataclass(frozen=True, slots=True)
class PaymentInput:
    distribution_id: str
    commitment_id: str
    amount_value: Decimal
    paid_at: str
    reference: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PaymentInput":
        return cls(
            distribution_id=identifier(raw.get("distribution_id"), "distribution_id"),
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            paid_at=iso_text(raw.get("paid_at"), "paid_at"),
            reference=required_text(raw.get("reference", ""), "reference", 128) if raw.get("reference") else "",
        )


@dataclass(frozen=True, slots=True)
class DisputeOpenInput:
    dispute_id: str
    scope: str
    round_id: str
    reason: str
    amount_value: Decimal
    frozen_share_percent: Decimal
    commitment_id: str | None
    window_id: str | None
    call_id: str | None
    distribution_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DisputeOpenInput":
        return cls(
            dispute_id=identifier(raw.get("dispute_id"), "dispute_id"),
            scope=choice(raw.get("scope"), "scope", DISPUTE_SCOPES),
            round_id=identifier(raw.get("round_id"), "round_id"),
            reason=required_text(raw.get("reason"), "reason", 1024),
            amount_value=amount(raw.get("amount", 0), "amount", minimum=Decimal("0")),
            frozen_share_percent=decimal_value(
                raw.get("frozen_share_percent", 100), "frozen_share_percent",
                minimum=Decimal("0"), maximum=Decimal("100"),
            ),
            commitment_id=optional_identifier(raw.get("commitment_id"), "commitment_id"),
            window_id=optional_identifier(raw.get("window_id"), "window_id"),
            call_id=optional_identifier(raw.get("call_id"), "call_id"),
            distribution_id=optional_identifier(raw.get("distribution_id"), "distribution_id"),
        )


@dataclass(frozen=True, slots=True)
class DisputeResolveInput:
    dispute_id: str
    resolution: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DisputeResolveInput":
        return cls(
            dispute_id=identifier(raw.get("dispute_id"), "dispute_id"),
            resolution=choice(raw.get("resolution"), "resolution", DISPUTE_RESOLUTIONS),
            note=required_text(raw.get("note", ""), "note", 1024) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class AdjustmentInput:
    adjustment_id: str
    kind: str
    round_id: str
    amount_value: Decimal
    reason: str
    commitment_id: str | None
    call_id: str | None
    distribution_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdjustmentInput":
        return cls(
            adjustment_id=identifier(raw.get("adjustment_id"), "adjustment_id"),
            kind=choice(raw.get("kind"), "kind", ADJUSTMENT_KINDS),
            round_id=identifier(raw.get("round_id"), "round_id"),
            amount_value=amount(raw.get("amount"), "amount"),
            reason=required_text(raw.get("reason"), "reason", 1024),
            commitment_id=optional_identifier(raw.get("commitment_id"), "commitment_id"),
            call_id=optional_identifier(raw.get("call_id"), "call_id"),
            distribution_id=optional_identifier(raw.get("distribution_id"), "distribution_id"),
        )
