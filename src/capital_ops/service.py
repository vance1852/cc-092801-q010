"""联合资本承诺、条件出资、争议冻结与周期结算的事务用例。

所有状态变更都以仅追加事件落账；当前余额、可调用额度、周期对账单和现金流依据
全部由 capital_ops.engine 从有效事件重放得到，争议解决或项目调整也只追加事件，
因此服务重启后可以无损恢复尚未解决的承诺冲突。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from . import engine
from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import ZERO, canonical_json, digest, event_hash, money_text, quantize_money
from .models import (
    AdjustmentInput,
    CapitalCallInput,
    CommitmentInput,
    ConditionInput,
    ConfirmationInput,
    DisputeOpenInput,
    DisputeResolveInput,
    DistributionInput,
    FollowOnExercise,
    FollowOnGrant,
    PartyInput,
    PaymentInput,
    ReceiptInput,
    RestrictionInput,
    RoundVersionInput,
    WindowInput,
)
from .storage import initialize, transaction


PARTICIPANT_PERMISSIONS = {
    "confirmation.write",
    "dispute.write",
    "follow_on.exercise",
    "report.read",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "fund_of_funds": set(PARTICIPANT_PERMISSIONS),
    "market_fund": set(PARTICIPANT_PERMISSIONS),
    "industrial": set(PARTICIPANT_PERMISSIONS),
    "gp": PARTICIPANT_PERMISSIONS
    | {
        "round.write",
        "commitment.write",
        "window.write",
        "condition.write",
        "follow_on.write",
        "restriction.write",
        "call.write",
        "receipt.write",
        "distribution.write",
        "payment.write",
    },
    "operator": {
        "round.write",
        "party.write",
        "commitment.write",
        "window.write",
        "condition.write",
        "follow_on.write",
        "restriction.write",
        "call.write",
        "receipt.write",
        "distribution.write",
        "payment.write",
        "dispute.write",
        "dispute.resolve",
        "adjustment.write",
        "round.close",
        "report.read",
    },
    "auditor": {"report.read", "audit.read"},
}


class CapitalSyndicateService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # -- 基础 ---------------------------------------------------------------

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM capital_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capital_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # -- 事件落账 -----------------------------------------------------------

    def _load_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT seq,event_type,entity_type,entity_id,actor_id,payload_json,previous_hash,event_hash,created_at "
            "FROM ledger_events ORDER BY seq"
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        return events

    def _state(self) -> dict[str, Any]:
        return engine.replay(self._load_events())

    def _record(
        self,
        records: Sequence[tuple[str, str, str, Mapping[str, Any]]],
        *,
        actor_id: str,
        idempotency: tuple[str, str, str, Callable[[list[dict[str, Any]]], Mapping[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        """在一个即时事务内追加多条哈希链事件（可选幂等结果）。"""

        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT event_hash FROM ledger_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            previous_hash = "0" * 64 if previous is None else previous["event_hash"]
            appended: list[dict[str, Any]] = []
            for event_type, entity_type, entity_id, payload in records:
                created_at = self._now()
                event_hash_value = event_hash(
                    previous_hash=previous_hash,
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    actor_id=actor_id,
                    payload=payload,
                    created_at=created_at,
                )
                cursor = self.connection.execute(
                    "INSERT INTO ledger_events(event_type,entity_type,entity_id,actor_id,payload_json,"
                    "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        event_type,
                        entity_type,
                        entity_id,
                        actor_id,
                        canonical_json(payload),
                        previous_hash,
                        event_hash_value,
                        created_at,
                    ),
                )
                previous_hash = event_hash_value
                appended.append(
                    {"seq": int(cursor.lastrowid), "event_hash": event_hash_value, "created_at": created_at}
                )
            if idempotency is not None:
                scope, key, request_sha256, response_factory = idempotency
                stored_response = dict(response_factory(appended))
                self.connection.execute(
                    "INSERT INTO capital_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, key, request_sha256, canonical_json(stored_response), self._now()),
                )
            return appended

    def _record_actor(
        self,
        actor_id: str,
        records: Sequence[tuple[str, str, str, Mapping[str, Any]]],
        *,
        idempotency: tuple[str, str, str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return self._record(records, actor_id=actor_id, idempotency=idempotency)

    def _replay_idempotent(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        request_sha256 = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM capital_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_sha256:
            raise Conflict("幂等键对应不同的请求内容")
        return {"response": json.loads(stored["response_json"]), "request_sha256": request_sha256}

    # -- 实体查找 -----------------------------------------------------------

    @staticmethod
    def _party(state: Mapping[str, Any], party_id: str) -> Mapping[str, Any]:
        party = state["parties"].get(party_id)
        if party is None:
            raise NotFound("参与方不存在")
        return party

    @staticmethod
    def _round(state: Mapping[str, Any], round_id: str) -> Mapping[str, Any]:
        round_record = state["rounds"].get(round_id)
        if round_record is None:
            raise NotFound("轮协议不存在")
        return round_record

    @staticmethod
    def _commitment(state: Mapping[str, Any], commitment_id: str) -> Mapping[str, Any]:
        commitment = state["commitments"].get(commitment_id)
        if commitment is None:
            raise NotFound("承诺不存在")
        return commitment

    @staticmethod
    def _window(state: Mapping[str, Any], window_id: str) -> Mapping[str, Any]:
        window = state["windows"].get(window_id)
        if window is None:
            raise NotFound("出资窗口不存在")
        return window

    def _assert_party_owner(self, user: sqlite3.Row, state: Mapping[str, Any], party_id: str) -> None:
        if user["role"] == "operator":
            return
        party = self._party(state, party_id)
        if user["user_id"] != party_id or user["role"] != party["kind"]:
            raise Forbidden("只能代表登记在本人名下的参与方行事")

    # -- 参与方与协议版本 ---------------------------------------------------

    def register_party(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        party = PartyInput.from_dict(raw)
        state = self._state()
        if party.party_id in state["parties"]:
            raise Conflict("参与方已经登记")
        payload = {
            "party_id": party.party_id,
            "display_name": party.display_name,
            "kind": party.kind,
        }
        appended = self._record_actor(actor_id, [("party.registered", "party", party.party_id, payload)])
        return {"party_id": party.party_id, "kind": party.kind, "event_seq": appended[0]["seq"]}

    def publish_round_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "round.write")
        version_input = RoundVersionInput.from_dict(raw)
        state = self._state()
        existing = state["rounds"].get(version_input.round_id)
        existing_versions = [] if existing is None else [item["version"] for item in existing["versions"]]
        expected = (max(existing_versions, default=0) + 1) if existing_versions else 1
        if version_input.version != expected:
            raise Conflict(f"协议版本必须顺序递增，下一个版本应为 {expected}")
        if version_input.supersedes_version is not None and existing_versions and version_input.supersedes_version != max(existing_versions):
            raise ValidationFailed("supersedes_version 必须指向当前最新版本")
        payload = {
            "round_id": version_input.round_id,
            "project_id": version_input.project_id,
            "version": version_input.version,
            "title": version_input.title,
            "effective_at": version_input.effective_at,
            "note": version_input.note,
            "supersedes_version": version_input.supersedes_version,
        }
        appended = self._record_actor(actor_id, [("round.version_published", "round", version_input.round_id, payload)])
        return {
            "round_id": version_input.round_id,
            "version": version_input.version,
            "state": "published",
            "event_seq": appended[0]["seq"],
        }

    # -- 承诺、窗口、条件、跟投权、用途限制 ---------------------------------

    def create_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        item = CommitmentInput.from_dict(raw)
        state = self._state()
        round_record = self._round(state, item.round_id)
        if item.commitment_id in state["commitments"]:
            raise Conflict("承诺编号已经存在")
        if item.round_version not in [entry["version"] for entry in round_record["versions"]]:
            raise ValidationFailed("承诺引用的协议版本尚未发布")
        party = self._party(state, item.party_id)
        payload = {
            "commitment_id": item.commitment_id,
            "round_id": item.round_id,
            "round_version": item.round_version,
            "party_id": item.party_id,
            "party_kind": party["kind"],
            "amount": money_text(item.amount_value),
            "currency": item.currency,
            "note": item.note,
        }
        appended = self._record_actor(actor_id, [("commitment.created", "commitment", item.commitment_id, payload)])
        line = engine.commitment_line(self._state(), item.commitment_id)
        line["event_seq"] = appended[0]["seq"]
        return line

    def attach_condition(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "condition.write")
        item = ConditionInput.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        if item.condition_id in state["conditions"]:
            raise Conflict("条件编号已经存在")
        if item.window_id is not None:
            window = state["windows"].get(item.window_id)
            if window is not None and window["round_id"] != item.round_id:
                raise ValidationFailed("窗口不属于该轮协议")
        if item.commitment_id is not None:
            commitment = self._commitment(state, item.commitment_id)
            if commitment["round_id"] != item.round_id:
                raise ValidationFailed("承诺不属于该轮协议")
        if item.depends_on is not None:
            dependency = state["conditions"].get(item.depends_on)
            if dependency is None or dependency["round_id"] != item.round_id:
                raise ValidationFailed("前置依赖条件不存在或不属于该轮协议")
        payload = {
            "condition_id": item.condition_id,
            "round_id": item.round_id,
            "scope": item.scope,
            "window_id": item.window_id,
            "commitment_id": item.commitment_id,
            "depends_on": item.depends_on,
            "title": item.title,
            "blocking": item.blocking,
        }
        appended = self._record_actor(actor_id, [("condition.attached", "condition", item.condition_id, payload)])
        return {"condition_id": item.condition_id, "state": "attached", "event_seq": appended[0]["seq"]}

    def satisfy_condition(self, actor_id: str, condition_id: str, evidence: str = "") -> dict[str, Any]:
        self._require(actor_id, "condition.write")
        state = self._state()
        condition = state["conditions"].get(condition_id)
        if condition is None:
            raise NotFound("条件不存在")
        if condition["satisfied"]:
            raise Conflict("条件已经满足")
        if condition["depends_on"]:  # 条件先后：依赖未满足不得勾选
            dependency = state["conditions"].get(condition["depends_on"])
            if dependency is None or not dependency["satisfied"]:
                raise InvalidState("必须先满足所依赖的前置条件")
        payload = {"condition_id": condition_id, "evidence": evidence or ""}
        appended = self._record_actor(actor_id, [("condition.satisfied", "condition", condition_id, payload)])
        return {"condition_id": condition_id, "state": "satisfied", "event_seq": appended[0]["seq"]}

    def open_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "window.write")
        item = WindowInput.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        if item.window_id in state["windows"]:
            raise Conflict("出资窗口编号已经存在")
        for condition_id in item.conditions:
            condition = state["conditions"].get(condition_id)
            if condition is None or condition["round_id"] != item.round_id:
                raise ValidationFailed(f"窗口引用的条件 {condition_id} 不存在或不属于该轮协议")
            if condition["scope"] != "window" or condition["window_id"] != item.window_id:
                raise ValidationFailed(f"条件 {condition_id} 不是绑定到本窗口的窗口级条件")
        for restriction_id in item.use_restrictions:
            restriction = state["restrictions"].get(restriction_id)
            if restriction is None or restriction["round_id"] != item.round_id:
                raise ValidationFailed(f"窗口引用的用途限制 {restriction_id} 不存在或不属于该轮协议")
        payload = {
            "window_id": item.window_id,
            "round_id": item.round_id,
            "title": item.title,
            "opens_at": item.opens_at,
            "closes_at": item.closes_at,
            "conditions": item.conditions,
            "use_restrictions": item.use_restrictions,
        }
        appended = self._record_actor(actor_id, [("window.opened", "window", item.window_id, payload)])
        return {"window_id": item.window_id, "state": "open", "event_seq": appended[0]["seq"]}

    def impose_restriction(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "restriction.write")
        item = RestrictionInput.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        if item.restriction_id in state["restrictions"]:
            raise Conflict("用途限制编号已经存在")
        if item.window_id is not None:
            window = state["windows"].get(item.window_id)
            if window is not None and window["round_id"] != item.round_id:
                raise ValidationFailed("窗口不属于该轮协议")
        if item.commitment_id is not None and self._commitment(state, item.commitment_id)["round_id"] != item.round_id:
            raise ValidationFailed("承诺不属于该轮协议")
        payload = {
            "restriction_id": item.restriction_id,
            "round_id": item.round_id,
            "window_id": item.window_id,
            "commitment_id": item.commitment_id,
            "title": item.title,
            "purpose": item.purpose,
        }
        appended = self._record_actor(actor_id, [("use_restriction.imposed", "restriction", item.restriction_id, payload)])
        return {"restriction_id": item.restriction_id, "event_seq": appended[0]["seq"]}

    def grant_follow_on(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "follow_on.write")
        item = FollowOnGrant.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        self._party(state, item.holder_party_id)
        if item.follow_on_id in state["follow_ons"]:
            raise Conflict("跟投权编号已经存在")
        if item.grant_commitment_id is not None:
            grant = self._commitment(state, item.grant_commitment_id)
            if grant["round_id"] != item.round_id or grant["party_id"] != item.holder_party_id:
                raise ValidationFailed("跟投权必须依附于持有人在本轮的既有承诺")
        if item.window_id is not None and self._window(state, item.window_id)["round_id"] != item.round_id:
            raise ValidationFailed("窗口不属于该轮协议")
        payload = {
            "follow_on_id": item.follow_on_id,
            "round_id": item.round_id,
            "holder_party_id": item.holder_party_id,
            "grant_commitment_id": item.grant_commitment_id,
            "share_percent": money_text(item.share_percent),
            "max_amount": None if item.max_amount_value is None else money_text(item.max_amount_value),
            "window_id": item.window_id,
            "expires_at": item.expires_at,
            "title": item.title,
        }
        appended = self._record_actor(actor_id, [("follow_on.granted", "follow_on", item.follow_on_id, payload)])
        return {"follow_on_id": item.follow_on_id, "state": "granted", "event_seq": appended[0]["seq"]}

    def exercise_follow_on(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        item = FollowOnExercise.from_dict(raw)
        state = self._state()
        follow_on = state["follow_ons"].get(item.follow_on_id)
        if follow_on is None:
            raise NotFound("跟投权不存在")
        user = self._user(actor_id)
        if user["role"] == "operator":
            self._require(actor_id, "follow_on.write")
        else:
            self._require(actor_id, "follow_on.exercise")
            self._assert_party_owner(user, state, follow_on["holder_party_id"])
        if follow_on["exercised"]:
            raise Conflict("跟投权已经行使")
        if follow_on["expires_at"] is not None and self._now() > follow_on["expires_at"]:
            raise InvalidState("跟投权已经过期")
        if follow_on["max_amount"] is not None and item.amount_value > Decimal(str(follow_on["max_amount"])):
            raise ValidationFailed("行使金额超过跟投权上限")
        round_record = self._round(state, follow_on["round_id"])
        if item.round_version not in [entry["version"] for entry in round_record["versions"]]:
            raise ValidationFailed("新承诺引用的协议版本尚未发布")
        if item.new_commitment_id in state["commitments"]:
            raise Conflict("新承诺编号已经存在")
        if item.into_window_id is not None and self._window(state, item.into_window_id)["round_id"] != follow_on["round_id"]:
            raise ValidationFailed("窗口不属于该轮协议")
        commitment_payload = {
            "commitment_id": item.new_commitment_id,
            "round_id": follow_on["round_id"],
            "round_version": item.round_version,
            "party_id": follow_on["holder_party_id"],
            "amount": money_text(item.amount_value),
            "currency": "CNY",
            "note": item.note or f"行使跟投权 {item.follow_on_id}",
            "origin": "follow_on",
        }
        exercise_payload = {
            "follow_on_id": item.follow_on_id,
            "amount": money_text(item.amount_value),
            "new_commitment_id": item.new_commitment_id,
            "into_window_id": item.into_window_id,
            "note": item.note,
        }
        # 持有人主动行使跟投权即视为确认其新承诺。
        confirm_payload = {
            "commitment_id": item.new_commitment_id,
            "party_id": follow_on["holder_party_id"],
            "decision": "confirmed",
            "confirmed_amount": money_text(quantize_money(item.amount_value)),
            "note": f"行使跟投权 {item.follow_on_id} 即确认",
        }
        appended = self._record_actor(actor_id,
            [
                ("commitment.created", "commitment", item.new_commitment_id, commitment_payload),
                ("commitment.confirmed", "commitment", item.new_commitment_id, confirm_payload),
                ("follow_on.exercised", "follow_on", item.follow_on_id, exercise_payload),
            ]
        )
        return {
            "follow_on_id": item.follow_on_id,
            "state": "exercised",
            "new_commitment_id": item.new_commitment_id,
            "event_seq": appended[-1]["seq"],
        }

    # -- 多方分别确认 -------------------------------------------------------

    def confirm_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        item = ConfirmationInput.from_dict(raw)
        user = self._user(actor_id)
        self._require(actor_id, "confirmation.write")
        state = self._state()
        commitment = self._commitment(state, item.commitment_id)
        self._assert_party_owner(user, state, commitment["party_id"])
        if any(entry["party_id"] == commitment["party_id"] for entry in commitment["confirmations"]):
            raise Conflict("该参与方已经确认过这份承诺")
        if item.decision == "confirmed":
            confirmed_amount = commitment["amount"] if item.confirmed_amount_value is None else item.confirmed_amount_value
            if confirmed_amount <= 0:
                raise ValidationFailed("确认金额必须大于零")
            if confirmed_amount > commitment["amount"]:
                raise ValidationFailed("确认金额不能超过承诺金额")
        else:
            confirmed_amount = ZERO
        payload = {
            "commitment_id": item.commitment_id,
            "party_id": commitment["party_id"],
            "decision": item.decision,
            "confirmed_amount": money_text(quantize_money(confirmed_amount)),
            "note": item.note,
        }
        appended = self._record_actor(actor_id, [("commitment.confirmed", "commitment", item.commitment_id, payload)])
        line = engine.commitment_line(self._state(), item.commitment_id)
        line["event_seq"] = appended[0]["seq"]
        return line

    # -- 调用与实缴 ---------------------------------------------------------

    def call_capital(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "call.write")
        item = CapitalCallInput.from_dict(raw)
        replayed = self._replay_idempotent("capital_call", item.idempotency_key, raw)
        if replayed is not None:
            return replayed["response"]
        state = self._state()
        commitment = self._commitment(state, item.commitment_id)
        window = self._window(state, item.window_id)
        if commitment["round_id"] != window["round_id"]:
            raise ValidationFailed("承诺与出资窗口不属于同一轮协议")
        if item.call_id in state["calls"]:
            raise Conflict("调用单编号已经存在")
        for restriction_id in item.use_restrictions:
            restriction = state["restrictions"].get(restriction_id)
            if restriction is None or restriction["round_id"] != window["round_id"]:
                raise ValidationFailed(f"用途限制 {restriction_id} 不存在或不属于该轮协议")
            applies = (
                restriction_id in window["use_restriction_ids"]
                or restriction.get("commitment_id") == item.commitment_id
                or (restriction.get("window_id") in (None, item.window_id))
            )
            if not applies:
                raise ValidationFailed(f"用途限制 {restriction_id} 不适用于该窗口或承诺")
        eligibility = engine.call_eligibility(
            state,
            commitment_id=item.commitment_id,
            window_id=item.window_id,
            amount=item.amount_value,
            as_of=self._now(),
        )
        if not eligibility["eligible"]:
            raise InvalidState("资金调用条件未满足", details=eligibility["reasons"])
        amount = quantize_money(item.amount_value)
        payload = {
            "call_id": item.call_id,
            "round_id": window["round_id"],
            "window_id": item.window_id,
            "commitment_id": item.commitment_id,
            "party_id": commitment["party_id"],
            "amount": money_text(amount),
            "purpose": item.purpose,
            "use_restriction_ids": item.use_restrictions,
            "idempotency_key": item.idempotency_key,
        }
        response = {
            "call_id": item.call_id,
            "state": "called",
            "amount": money_text(amount),
            "window_id": item.window_id,
            "commitment_id": item.commitment_id,
            "replayed": False,
        }

        def response_factory(appended_events: list[dict[str, Any]]) -> Mapping[str, Any]:
            return {**response, "event_seq": appended_events[0]["seq"]}

        self._record_actor(
            actor_id,
            [("capital.called", "call", item.call_id, payload)],
            idempotency=("capital_call", item.idempotency_key, digest(raw), response_factory),
        )
        stored = self._replay_idempotent("capital_call", item.idempotency_key, raw)
        assert stored is not None
        return stored["response"]

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        item = ReceiptInput.from_dict(raw)
        state = self._state()
        call = state["calls"].get(item.call_id)
        if call is None:
            raise NotFound("调用单不存在")
        if any(receipt["receipt_id"] == item.receipt_id for receipt in state["receipts"]):
            raise Conflict("实缴凭证编号已经存在")
        amount = quantize_money(item.amount_value)
        outstanding = quantize_money(call["amount"] - call["received_amount"])
        if amount <= ZERO:
            raise ValidationFailed("实缴金额必须大于零")
        blocking = engine.disputes_blocking_receipt(
            state, round_id=call["round_id"], call_id=item.call_id
        )
        for dispute in blocking:
            raise InvalidState(f"争议 {dispute['dispute_id']} 冻结了该调用单，暂停实缴：{dispute['reason']}")
        if amount > outstanding:
            raise Conflict(f"实缴金额超过调用单未收余额 {money_text(outstanding)}")
        payload = {
            "receipt_id": item.receipt_id,
            "call_id": item.call_id,
            "commitment_id": call["commitment_id"],
            "amount": money_text(amount),
            "received_at": item.received_at,
            "reference": item.reference,
        }
        appended = self._record_actor(actor_id, [("capital.received", "receipt", item.receipt_id, payload)])
        return {
            "receipt_id": item.receipt_id,
            "call_id": item.call_id,
            "amount": money_text(amount),
            "state": "received",
            "event_seq": appended[0]["seq"],
        }

    # -- 分配与返还 ---------------------------------------------------------

    def declare_distribution(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "distribution.write")
        item = DistributionInput.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        if item.distribution_id in state["distributions"]:
            raise Conflict("分配决议编号已经存在")
        disputed_calls = engine.disputed_call_ids(state, item.round_id)
        if item.window_id is not None and self._window(state, item.window_id)["round_id"] != item.round_id:
            raise ValidationFailed("窗口不属于该轮协议")
        total = ZERO
        allocations_payload = []
        for allocation in item.allocations:
            commitment = self._commitment(state, allocation.commitment_id)
            if commitment["round_id"] != item.round_id:
                raise ValidationFailed(f"承诺 {allocation.commitment_id} 不属于该轮协议")
            if commitment["party_id"] != allocation.party_id:
                raise ValidationFailed(f"分配参与方与承诺 {allocation.commitment_id} 持有人不一致")
            line = engine.commitment_line(state, allocation.commitment_id)
            if item.category == "return_of_capital" and allocation.amount_value > Decimal(line["received_amount"]):
                raise InvalidState(
                    f"返还本金不能超过 {allocation.commitment_id} 的实缴金额 {line['received_amount']}"
                )
            for call_id in allocation.source_call_ids:
                source_call = state["calls"].get(call_id)
                if source_call is None or source_call["commitment_id"] != allocation.commitment_id:
                    raise ValidationFailed(f"来源调用单 {call_id} 不存在或不属于该承诺")
                if call_id in disputed_calls:
                    raise InvalidState(f"来源调用单 {call_id} 处于争议中，暂不得据此分配返还")
            total += allocation.amount_value
            allocations_payload.append(
                {
                    "commitment_id": allocation.commitment_id,
                    "party_id": allocation.party_id,
                    "amount": money_text(quantize_money(allocation.amount_value)),
                    "source_call_ids": allocation.source_call_ids,
                }
            )
        if quantize_money(total) != quantize_money(item.amount_value):
            raise ValidationFailed("分配明细之和必须等于分配总额")
        payload = {
            "distribution_id": item.distribution_id,
            "round_id": item.round_id,
            "window_id": item.window_id,
            "title": item.title,
            "category": item.category,
            "amount": money_text(quantize_money(item.amount_value)),
            "payable_at": item.payable_at,
            "allocations": allocations_payload,
        }
        appended = self._record_actor(actor_id, 
            [("distribution.declared", "distribution", item.distribution_id, payload)]
        )
        return {
            "distribution_id": item.distribution_id,
            "state": "declared",
            "amount": money_text(quantize_money(item.amount_value)),
            "allocations": len(allocations_payload),
            "event_seq": appended[0]["seq"],
        }

    def record_payment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "payment.write")
        item = PaymentInput.from_dict(raw)
        state = self._state()
        distribution = state["distributions"].get(item.distribution_id)
        if distribution is None:
            raise NotFound("分配决议不存在")
        allocation = distribution["allocations"].get(item.commitment_id)
        if allocation is None:
            raise NotFound("该参与方不在分配明细中")
        if allocation["party_id"] != (state["commitments"][item.commitment_id]["party_id"]):
            raise ValidationFailed("承诺参与方与分配明细不一致")
        amount = quantize_money(item.amount_value)
        if amount <= ZERO:
            raise ValidationFailed("支付金额必须大于零")
        remaining = quantize_money(allocation["amount"] - allocation["paid_amount"])
        if amount > remaining:
            raise Conflict(f"支付金额超过该份额未付余额 {money_text(remaining)}")
        blocking_disputes = engine.disputes_blocking_payment(
            state,
            round_id=distribution["round_id"],
            distribution_id=item.distribution_id,
            commitment_id=item.commitment_id,
        )
        for dispute in blocking_disputes:
            raise InvalidState(
                f"争议 {dispute['dispute_id']} 仍在冻结该份额，暂停支付：{dispute['reason']}"
            )
        payload = {
            "distribution_id": item.distribution_id,
            "commitment_id": item.commitment_id,
            "party_id": allocation["party_id"],
            "amount": money_text(amount),
            "paid_at": item.paid_at,
            "reference": item.reference,
        }
        appended = self._record_actor(actor_id, [("distribution.paid", "payment", item.distribution_id, payload)])
        return {
            "distribution_id": item.distribution_id,
            "commitment_id": item.commitment_id,
            "amount": money_text(amount),
            "state": "paid",
            "event_seq": appended[0]["seq"],
        }

    # -- 争议：只冻结相关承诺与金额 ----------------------------------------

    def open_dispute(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        item = DisputeOpenInput.from_dict(raw)
        user = self._user(actor_id)
        self._require(actor_id, "dispute.write")
        state = self._state()
        self._round(state, item.round_id)
        if any(dispute["dispute_id"] == item.dispute_id for dispute in state["disputes"]):
            raise Conflict("争议编号已经存在")

        target_party_id: str | None = None
        cap_amount: Decimal | None = None
        linked_commitment_id = item.commitment_id
        if item.scope == "commitment":
            if item.commitment_id is None:
                raise ValidationFailed("承诺级争议必须提供 commitment_id")
            commitment = self._commitment(state, item.commitment_id)
            if commitment["round_id"] != item.round_id:
                raise ValidationFailed("承诺不属于该轮协议")
            target_party_id = commitment["party_id"]
            cap_amount = commitment["amount"]
        elif item.scope == "window":
            window = self._window(state, item.window_id)
            if window["round_id"] != item.round_id:
                raise ValidationFailed("窗口不属于该轮协议")
            if item.commitment_id is not None:
                target_party_id = self._commitment(state, item.commitment_id)["party_id"]
        elif item.scope == "call":
            call = state["calls"].get(item.call_id)
            if call is None or call["round_id"] != item.round_id:
                raise ValidationFailed("调用单不存在或不属于该轮协议")
            target_party_id = call["party_id"]
            cap_amount = call["amount"]
            linked_commitment_id = call["commitment_id"]
        elif item.scope == "distribution":
            distribution = state["distributions"].get(item.distribution_id)
            if distribution is None or distribution["round_id"] != item.round_id:
                raise ValidationFailed("分配决议不存在或不属于该轮协议")
            if item.commitment_id is None:
                raise ValidationFailed("分配争议必须提供 commitment_id")
            allocation = distribution["allocations"].get(item.commitment_id)
            if allocation is None:
                raise ValidationFailed("该承诺不在分配明细中")
            target_party_id = allocation["party_id"]
            cap_amount = quantize_money(allocation["amount"] - allocation["paid_amount"])
        elif item.scope == "round":
            if item.commitment_id is not None:
                target_party_id = self._commitment(state, item.commitment_id)["party_id"]

        if target_party_id is not None and user["role"] not in {"operator", "gp"}:
            self._assert_party_owner(user, state, target_party_id)
        if cap_amount is not None and item.amount_value > cap_amount:
            raise ValidationFailed(f"争议冻结金额不能超过相关金额 {money_text(quantize_money(cap_amount))}")

        payload = {
            "dispute_id": item.dispute_id,
            "scope": item.scope,
            "round_id": item.round_id,
            "commitment_id": linked_commitment_id,
            "window_id": item.window_id,
            "call_id": item.call_id,
            "distribution_id": item.distribution_id,
            "amount": money_text(quantize_money(item.amount_value)),
            "frozen_share_percent": money_text(item.frozen_share_percent),
            "reason": item.reason,
        }
        appended = self._record_actor(actor_id, [("dispute.opened", "dispute", item.dispute_id, payload)])
        frozen = quantize_money(item.amount_value * item.frozen_share_percent / Decimal("100"))
        return {
            "dispute_id": item.dispute_id,
            "state": "open",
            "frozen_amount": money_text(frozen),
            "event_seq": appended[0]["seq"],
        }

    def resolve_dispute(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dispute.resolve")
        item = DisputeResolveInput.from_dict(raw)
        state = self._state()
        dispute = next((entry for entry in state["disputes"] if entry["dispute_id"] == item.dispute_id), None)
        if dispute is None:
            raise NotFound("争议不存在")
        if dispute["resolved_at"] is not None:
            raise Conflict("争议已经解决")
        records: list[tuple[str, str, str, Mapping[str, Any]]] = [
            (
                "dispute.resolved",
                "dispute",
                item.dispute_id,
                {"dispute_id": item.dispute_id, "resolution": item.resolution, "note": item.note},
            )
        ]
        appended = self._record_actor(actor_id, records)
        return {
            "dispute_id": item.dispute_id,
            "state": "resolved",
            "resolution": item.resolution,
            "event_seq": appended[0]["seq"],
        }

    # -- 项目调整：只追加原因，不改写历史 -----------------------------------

    def append_adjustment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "adjustment.write")
        item = AdjustmentInput.from_dict(raw)
        state = self._state()
        self._round(state, item.round_id)
        if any(entry["adjustment_id"] == item.adjustment_id for entry in state["adjustments"]):
            raise Conflict("调整决定编号已经存在")
        if item.commitment_id is not None and self._commitment(state, item.commitment_id)["round_id"] != item.round_id:
            raise ValidationFailed("承诺不属于该轮协议")
        if item.call_id is not None and (
            item.call_id not in state["calls"] or state["calls"][item.call_id]["round_id"] != item.round_id
        ):
            raise ValidationFailed("调用单不存在或不属于该轮协议")
        if item.distribution_id is not None and (
            item.distribution_id not in state["distributions"]
            or state["distributions"][item.distribution_id]["round_id"] != item.round_id
        ):
            raise ValidationFailed("分配决议不存在或不属于该轮协议")
        if item.kind in {"write_off", "cancelled"} and item.commitment_id is not None:
            line = engine.commitment_line(state, item.commitment_id)
            removable = Decimal(line["effective_commitment"]) - Decimal(line["called_amount"])
            if item.amount_value > max(ZERO, removable):
                raise ValidationFailed(f"核减金额不能超过尚未调用的承诺余额 {money_text(removable)}")
        payload = {
            "adjustment_id": item.adjustment_id,
            "scope": "commitment" if item.commitment_id else "round",
            "round_id": item.round_id,
            "commitment_id": item.commitment_id,
            "call_id": item.call_id,
            "distribution_id": item.distribution_id,
            "kind": item.kind,
            "amount": money_text(quantize_money(item.amount_value)),
            "reason": item.reason,
        }
        appended = self._record_actor(actor_id, [("adjustment.appended", "adjustment", item.adjustment_id, payload)])
        return {
            "adjustment_id": item.adjustment_id,
            "kind": item.kind,
            "state": "appended",
            "event_seq": appended[0]["seq"],
        }

    def close_round(self, actor_id: str, round_id: str) -> dict[str, Any]:
        self._require(actor_id, "round.close")
        state = self._state()
        self._round(state, round_id)
        if state["rounds"][round_id]["closed_at"] is not None:
            raise Conflict("该轮协议已经关闭")
        appended = self._record_actor(actor_id, [("round.closed", "round", round_id, {"round_id": round_id})])
        return {"round_id": round_id, "state": "closed", "event_seq": appended[0]["seq"]}

    # -- 查询、核算与解释 ---------------------------------------------------

    def round_statement(self, actor_id: str, round_id: str, as_of: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        state = engine.replay(self._load_events(), as_of=as_of)
        if round_id not in state["rounds"]:
            raise NotFound("轮协议不存在")
        return engine.round_statement(state, round_id)

    def commitment_status(self, actor_id: str, commitment_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        state = self._state()
        self._commitment(state, commitment_id)
        return engine.commitment_line(state, commitment_id)

    def eligibility_preview(self, actor_id: str, commitment_id: str, window_id: str, amount: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        state = self._state()
        self._commitment(state, commitment_id)
        self._window(state, window_id)
        return engine.call_eligibility(
            state,
            commitment_id=commitment_id,
            window_id=window_id,
            amount=Decimal(str(amount)),
            as_of=self._now(),
        )

    def explain_contribution(self, actor_id: str, receipt_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        state = self._state()
        try:
            return engine.explain_receipt(state, receipt_id)
        except KeyError as exc:
            raise NotFound("实缴凭证不存在") from exc

    def explain_return(self, actor_id: str, distribution_id: str, commitment_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        state = self._state()
        try:
            return engine.explain_payment(state, distribution_id, commitment_id)
        except KeyError as exc:
            raise NotFound("返还记录不存在") from exc

    def round_events(self, actor_id: str, round_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        state = self._state()
        self._round(state, round_id)
        events = [
            {
                "seq": event["seq"],
                "event_type": event["event_type"],
                "entity_type": event["entity_type"],
                "entity_id": event["entity_id"],
                "actor_id": event["actor_id"],
                "payload": event["payload"],
                "created_at": event["created_at"],
            }
            for event in self._load_events()
        ]
        related = [
            event
            for event in events
            if event["entity_type"] in {"round", "party"}
            or event["payload"].get("round_id") == round_id
        ]
        return {"round_id": round_id, "events": related}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM ledger_events ORDER BY seq").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            calculated = event_hash(
                previous_hash=row["previous_hash"],
                event_type=row["event_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                actor_id=row["actor_id"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    def recover_unresolved_disputes(self) -> dict[str, Any]:
        """重启后重放事件，恢复所有尚未解决的承诺冲突与冻结金额。"""

        state = self._state()
        disputes = []
        for dispute in engine.active_disputes(state):
            line = None
            if dispute["commitment_id"]:
                line = engine.commitment_line(state, dispute["commitment_id"])
            disputes.append(
                {
                    "dispute_id": dispute["dispute_id"],
                    "scope": dispute["scope"],
                    "round_id": dispute["round_id"],
                    "commitment_id": dispute["commitment_id"],
                    "window_id": dispute["window_id"],
                    "call_id": dispute["call_id"],
                    "distribution_id": dispute["distribution_id"],
                    "claimed_amount": money_text(dispute["amount"]),
                    "frozen_share_percent": money_text(dispute["frozen_share_percent"]),
                    "frozen_amount": money_text(
                        quantize_money(dispute["amount"] * dispute["frozen_share_percent"] / Decimal("100"))
                    ),
                    "reason": dispute["reason"],
                    "opened_by": dispute["opened_by"],
                    "opened_at": dispute["opened_at"],
                    "commitment": line,
                }
            )
        return {
            "open_disputes": disputes,
            "count": len(disputes),
            "events_replayed": state["events_applied"],
            "recovered_at": self._now(),
        }
