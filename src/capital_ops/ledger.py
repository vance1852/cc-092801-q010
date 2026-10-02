"""联合资本账本的规范化序列化、内容摘要与事件类型常量。

账本事件是唯一事实来源：所有当前状态都由这些仅追加事件重放得到，
任何周期核算都必须能从有效事件序列重新计算出完全相同的结果。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP


MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")

# 事件类型即协议语言；新增事件只能追加，不能改写既有事件含义。
EVENT_TYPES = frozenset(
    {
        "party.registered",
        "round.version_published",
        "commitment.created",
        "window.opened",
        "condition.attached",
        "condition.satisfied",
        "follow_on.granted",
        "follow_on.exercised",
        "use_restriction.imposed",
        "commitment.confirmed",
        "capital.called",
        "capital.received",
        "distribution.declared",
        "distribution.paid",
        "dispute.opened",
        "dispute.resolved",
        "adjustment.appended",
        "round.closed",
    }
)

CONFIRMATION_DECISIONS = frozenset({"confirmed", "rejected"})
DISPUTE_SCOPES = frozenset({"commitment", "window", "call", "distribution", "round"})
DISPUTE_RESOLUTIONS = frozenset({"released", "upheld", "split"})
ADJUSTMENT_KINDS = frozenset({"write_off", "reinstated", "cancelled", "reallocation"})


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def money_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_hash(*, previous_hash: str, event_type: str, entity_type: str, entity_id: str,
               actor_id: str, payload: object, created_at: str) -> str:
    """按稳定字段顺序计算事件哈希，形成防篡改哈希链。"""

    body = {
        "previous_hash": previous_hash,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor_id": actor_id,
        "payload": payload,
        "created_at": created_at,
    }
    return digest(body)
