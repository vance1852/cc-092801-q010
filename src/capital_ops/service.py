"""联合资本承诺与结算的事务用例。

职责：
- 逐轮保存协议版本（承诺、出资窗口、先决条件、跟投权、用途/退出限制），调整只追加新版本并记录原因；
- 参与方用户分别确认自己负责的承诺片段；
- 调用资本时按窗口开放、先决条件与争议冻结切分可调用/被阻金额，未满足条件的金额不得调用；
- 争议只冻结相关承诺和金额，无争议部分继续出资与返还；
- 周期台账始终从有效现金流事件（booked）重新计算，并解释每笔投入与返还的依据。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .accounting import (
    WindowBlocker,
    ZERO,
    allocate_return,
    canonical_json,
    decimal_text,
    digest,
    evaluate_windows,
    money,
    period_totals,
    quantize_money,
)
from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import AgreementRevision, CashEventInput, DisputeSpec
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "manager": {
        "project.write", "revision.write", "call.write", "cash.write",
        "dispute.write", "dispute.resolve", "report.read",
    },
    "party": {
        "confirmation.write", "dispute.write", "cash.write", "report.read",
    },
    "risk": {"condition.decide", "dispute.resolve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

CONDITION_SCOPES = {"commitment", "window", "condition", "right", "restriction"}
ADJUSTMENT_KINDS = {"round_add", "party_add", "party_exit", "term_change", "restart"}


class CapitalSyndicateService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ---------- 主体与权限 ----------

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
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(
        self, user_id: str, display_name: str, role: str, party_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "party" and not (party_id or "").strip():
            raise ValidationFailed("参与方用户必须绑定 party_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capital_users(user_id,display_name,role,party_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, party_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "party_id": party_id}

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM capital_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO capital_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, request_payload: Mapping[str, Any]) -> dict[str, Any] | None:
        request_digest = digest(request_payload)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM capital_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_payload: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO capital_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(request_payload), canonical_json(response), self._now()),
        )

    # ---------- 项目与协议版本 ----------

    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project_id = raw.get("project_id")
        name = raw.get("name")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValidationFailed("project_id 不能为空")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name 不能为空")
        project_id = project_id.strip()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO projects(project_id,name,created_by,created_at) VALUES(?,?,?,?)",
                    (project_id, name.strip(), actor_id, self._now()),
                )
                self._audit("project", project_id, "project.created", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号已经存在") from exc
        return {"project_id": project_id, "current_revision": 0}

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        return row

    def _current_revision(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agreement_revisions WHERE project_id=? AND state='effective' "
            "ORDER BY revision DESC LIMIT 1",
            (project_id,),
        ).fetchone()

    def publish_revision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "revision.write")
        project_id = str(raw.get("project_id", "")).strip()
        self._project(project_id)
        previous = self._current_revision(project_id)
        next_revision_number = 1 if previous is None else int(previous["revision"]) + 1
        if previous is not None:
            reason = raw.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationFailed("追加协议版本必须填写调整原因 reason")
            adjustment_kind = str(raw.get("adjustment_kind", "term_change"))
            if adjustment_kind not in ADJUSTMENT_KINDS:
                raise ValidationFailed("adjustment_kind 不是受支持的调整类型")
        else:
            adjustment_kind = None
        agreement = AgreementRevision.from_dict(raw, next_revision_number, None if previous is None else int(previous["revision_id"]))
        content = canonical_json(raw)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        response: dict[str, Any] = {}
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO agreement_revisions(project_id,round_code,revision,basis_revision_id,reason,"
                    "content_json,content_sha256,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,'effective',?,?)",
                    (
                        project_id, agreement.round_code, agreement.revision,
                        None if previous is None else previous["revision_id"],
                        agreement.reason, content, content_sha256, actor_id, self._now(),
                    ),
                )
                revision_id = int(cursor.lastrowid)
                for spec in agreement.commitments:
                    self._insert_commitment(revision_id, spec)
                if previous is not None:
                    self.connection.execute(
                        "UPDATE agreement_revisions SET state='superseded',superseded_at=? WHERE revision_id=?",
                        (self._now(), previous["revision_id"]),
                    )
                    self.connection.execute(
                        "UPDATE projects SET current_revision=? WHERE project_id=?",
                        (agreement.revision, project_id),
                    )
                    self.connection.execute(
                        "INSERT INTO project_addendums(project_id,previous_revision_id,next_revision_id,"
                        "adjustment_kind,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            project_id, previous["revision_id"], revision_id,
                            adjustment_kind, agreement.reason, actor_id, self._now(),
                        ),
                    )
                else:
                    self.connection.execute(
                        "UPDATE projects SET current_revision=1 WHERE project_id=?", (project_id,)
                    )
                self._audit(
                    "agreement_revision", str(revision_id), "agreement_revision.published",
                    actor_id,
                    {"project_id": project_id, "revision": agreement.revision, "sha256": content_sha256,
                     "commitments": len(agreement.commitments), **({"adjustment_kind": adjustment_kind} if adjustment_kind else {})},
                )
                response = {
                    "project_id": project_id,
                    "revision_id": revision_id,
                    "revision": agreement.revision,
                    "round_code": agreement.round_code,
                    "state": "effective",
                    "sha256": content_sha256,
                    "party_count": len(agreement.commitments),
                }
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本保存冲突") from exc
        return response

    def _insert_commitment(self, revision_id: int, spec: Any) -> int:
        cursor = self.connection.execute(
            "INSERT INTO party_commitments(revision_id,party_id,party_kind,committed_amount) VALUES(?,?,?,?)",
            (revision_id, spec.party_id, spec.party_kind, decimal_text(spec.committed_amount)),
        )
        commitment_id = int(cursor.lastrowid)
        for window in spec.windows:
            self.connection.execute(
                "INSERT INTO funding_windows(commitment_id,window_code,window_type,opens_on,closes_on,"
                "amount,order_index) VALUES(?,?,?,?,?,?,?)",
                (
                    commitment_id, window.window_id, window.window_type, window.opens_on,
                    window.closes_on, decimal_text(window.amount), window.order_index,
                ),
            )
        window_code_to_id = {
            row["window_code"]: int(row["funding_window_id"])
            for row in self.connection.execute(
                "SELECT funding_window_id,window_code FROM funding_windows WHERE commitment_id=?",
                (commitment_id,),
            )
        }
        for condition in spec.conditions:
            gate = None if condition.gate_amount is None else decimal_text(condition.gate_amount)
            cursor = self.connection.execute(
                "INSERT INTO commitment_conditions(commitment_id,condition_code,condition_type,label,gate_amount) "
                "VALUES(?,?,?,?,?)",
                (commitment_id, condition.condition_id, condition.condition_type, condition.label, gate),
            )
            condition_row_id = int(cursor.lastrowid)
            for window_code in condition.blocking_window_ids:
                self.connection.execute(
                    "INSERT INTO condition_blocked_windows(condition_id,funding_window_id) VALUES(?,?)",
                    (condition_row_id, window_code_to_id[window_code]),
                )
        for right in spec.follow_on_rights:
            self.connection.execute(
                "INSERT INTO follow_on_rights(commitment_id,right_code,future_round_code,pro_rata_percent,"
                "exercise_window_days) VALUES(?,?,?,?,?)",
                (
                    commitment_id, right.right_id, right.future_round_code,
                    decimal_text(right.pro_rata_percent), right.exercise_window_days,
                ),
            )
        for restriction in spec.restrictions:
            self.connection.execute(
                "INSERT INTO use_restrictions(commitment_id,restriction_code,restriction_type,"
                "allowed_categories_json,notice_required,note) VALUES(?,?,?,?,?,?)",
                (
                    commitment_id, restriction.restriction_id, restriction.restriction_type,
                    canonical_json(list(restriction.allowed_categories)),
                    1 if restriction.notice_required else 0, restriction.note,
                ),
            )
        return commitment_id

    def agreement(self, actor_id: str, project_id: str, revision: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._project(project_id)
        row = self._revision_row(project_id, revision)
        commitments = []
        for commitment in self.connection.execute(
            "SELECT * FROM party_commitments WHERE revision_id=? ORDER BY party_id", (row["revision_id"],)
        ):
            windows = [
                dict(item) for item in self.connection.execute(
                    "SELECT * FROM funding_windows WHERE commitment_id=? ORDER BY order_index",
                    (commitment["commitment_id"],),
                )
            ]
            conditions = []
            for condition in self.connection.execute(
                "SELECT * FROM commitment_conditions WHERE commitment_id=? ORDER BY condition_code",
                (commitment["commitment_id"],),
            ):
                blocked = [
                    blocked_row["window_code"]
                    for blocked_row in self.connection.execute(
                        "SELECT w.window_code FROM condition_blocked_windows b "
                        "JOIN funding_windows w ON w.funding_window_id=b.funding_window_id WHERE b.condition_id=?",
                        (condition["condition_id"],),
                    )
                ]
                conditions.append({**dict(condition), "blocking_window_codes": blocked})
            rights = [
                dict(item) for item in self.connection.execute(
                    "SELECT * FROM follow_on_rights WHERE commitment_id=? ORDER BY right_code",
                    (commitment["commitment_id"],),
                )
            ]
            restrictions = [
                dict(item) for item in self.connection.execute(
                    "SELECT * FROM use_restrictions WHERE commitment_id=? ORDER BY restriction_code",
                    (commitment["commitment_id"],),
                )
            ]
            confirmations = [
                dict(item) for item in self.connection.execute(
                    "SELECT scope,item_ref,confirmed_by,note,created_at FROM party_confirmations "
                    "WHERE revision_id=? AND party_id=? ORDER BY confirmation_id",
                    (row["revision_id"], commitment["party_id"]),
                )
            ]
            commitments.append({
                **dict(commitment),
                "funding_windows": windows,
                "conditions": conditions,
                "follow_on_rights": rights,
                "restrictions": restrictions,
                "confirmations": confirmations,
            })
        addendums = [
            dict(item) for item in self.connection.execute(
                "SELECT a.* FROM project_addendums a WHERE a.project_id=? ORDER BY a.addendum_id",
                (project_id,),
            )
        ]
        return {
            "project_id": project_id,
            "revision_id": row["revision_id"],
            "revision": row["revision"],
            "round_code": row["round_code"],
            "state": row["state"],
            "reason": row["reason"],
            "basis_revision_id": row["basis_revision_id"],
            "content_sha256": row["content_sha256"],
            "created_at": row["created_at"],
            "commitments": commitments,
            "addendums": addendums,
        }

    def _revision_row(self, project_id: str, revision: int | None) -> sqlite3.Row:
        if revision is None:
            row = self._current_revision(project_id)
        else:
            row = self.connection.execute(
                "SELECT * FROM agreement_revisions WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return row

    # ---------- 参与方分别确认 ----------

    def confirm_part(
        self,
        actor_id: str,
        project_id: str,
        party_id: str,
        scope: str,
        item_ref: str | None,
        note: str | None = None,
    ) -> dict[str, Any]:
        user = self._require(actor_id, "confirmation.write")
        if user["party_id"] != party_id:
            raise Forbidden("参与方只能确认本机构负责的承诺部分")
        if scope not in CONDITION_SCOPES:
            raise ValidationFailed("scope 必须是 commitment、window、condition、right 或 restriction")
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        commitment = self._commitment(revision["revision_id"], party_id)
        self._require_own_part(commitment, scope, item_ref)
        existing = self.connection.execute(
            "SELECT 1 FROM party_confirmations WHERE revision_id=? AND party_id=? AND scope=? "
            "AND (item_ref IS ? OR item_ref=?)",
            (revision["revision_id"], party_id, scope, item_ref, item_ref),
        ).fetchone()
        if existing is not None:
            raise Conflict("该承诺部分已经确认")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO party_confirmations(revision_id,party_id,scope,item_ref,confirmed_by,note,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (revision["revision_id"], party_id, scope, item_ref, actor_id, note, self._now()),
            )
            if scope == "commitment":
                self.connection.execute(
                    "UPDATE party_commitments SET state='confirmed',confirmed_at=? WHERE commitment_id=?",
                    (self._now(), commitment["commitment_id"]),
                )
            self._audit(
                "commitment", str(commitment["commitment_id"]), "commitment_part.confirmed",
                actor_id, {"project_id": project_id, "party_id": party_id, "scope": scope, "item_ref": item_ref},
            )
        return {
            "project_id": project_id, "revision": revision["revision"], "party_id": party_id,
            "scope": scope, "item_ref": item_ref, "state": "confirmed",
        }

    def _require_own_part(self, commitment: sqlite3.Row, scope: str, item_ref: str | None) -> None:
        commitment_id = commitment["commitment_id"]
        if scope == "commitment":
            if item_ref is not None:
                raise ValidationFailed("确认整份承诺时 item_ref 必须为空")
            return
        if item_ref is None:
            raise ValidationFailed("确认具体条款时必须提供 item_ref")
        table = {
            "window": ("funding_windows", "window_code"),
            "condition": ("commitment_conditions", "condition_code"),
            "right": ("follow_on_rights", "right_code"),
            "restriction": ("use_restrictions", "restriction_code"),
        }[scope]
        row = self.connection.execute(
            f"SELECT 1 FROM {table[0]} WHERE commitment_id=? AND {table[1]}=?",
            (commitment_id, item_ref),
        ).fetchone()
        if row is None:
            raise NotFound(f"参与方承诺下不存在该 {scope} 条款")

    # ---------- 先决条件裁量 ----------

    def decide_condition(
        self,
        actor_id: str,
        project_id: str,
        party_id: str,
        condition_code: str,
        status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "condition.decide")
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        if status not in {"satisfied", "waived", "failed"}:
            raise ValidationFailed("条件状态必须是 satisfied、waived 或 failed")
        commitment = self._commitment(revision["revision_id"], party_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE commitment_conditions SET status=?,decided_by=?,decided_at=?,note=? "
                "WHERE commitment_id=? AND condition_code=?",
                (status, actor_id, self._now(), note, commitment["commitment_id"], condition_code),
            )
            if cursor.rowcount != 1:
                raise NotFound("先决条件不存在")
            self._audit(
                "condition", condition_code, "condition.decided", actor_id,
                {"project_id": project_id, "party_id": party_id, "status": status},
            )
        return {"project_id": project_id, "party_id": party_id, "condition_code": condition_code, "status": status}

    def _commitment(self, revision_id: int, party_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM party_commitments WHERE revision_id=? AND party_id=?",
            (revision_id, party_id),
        ).fetchone()
        if row is None:
            raise NotFound("当前协议版本中不存在该参与方承诺")
        return row

    # ---------- 窗口状态、条件门控与争议冻结 ----------

    def _standings(
        self, revision_id: int, commitment: sqlite3.Row, as_of: str
    ) -> tuple[list[Any], Decimal, Decimal]:
        """返回 (窗口状态列表, 窗口级争议冻结合计, 承诺级争议冻结金额)。"""
        windows = list(self.connection.execute(
            "SELECT * FROM funding_windows WHERE commitment_id=? ORDER BY order_index",
            (commitment["commitment_id"],),
        ))
        blockers: dict[int, list[WindowBlocker]] = {}
        condition_rows = self.connection.execute(
            "SELECT c.condition_id,c.condition_code,c.status,c.gate_amount,b.funding_window_id "
            "FROM commitment_conditions c JOIN condition_blocked_windows b ON b.condition_id=c.condition_id "
            "WHERE c.commitment_id=? AND c.status IN ('pending','failed')",
            (commitment["commitment_id"],),
        ).fetchall()
        for row in condition_rows:
            blockers.setdefault(int(row["funding_window_id"]), []).append(
                WindowBlocker(
                    condition_code=row["condition_code"],
                    status=row["status"],
                    gate_amount=None if row["gate_amount"] is None else money(row["gate_amount"]),
                )
            )
        contributed: dict[int, Decimal] = {}
        for row in self.connection.execute(
            "SELECT w.funding_window_id,COALESCE(SUM(CAST(e.amount AS REAL)),0) AS amount "
            "FROM funding_windows w JOIN cash_events e ON e.funding_window_id=w.funding_window_id "
            "WHERE w.commitment_id=? AND e.state='booked' AND e.direction='contribution' GROUP BY w.funding_window_id",
            (commitment["commitment_id"],),
        ):
            contributed[int(row["funding_window_id"])] = money(row["amount"])
        window_frozen: dict[int, Decimal] = {}
        commitment_frozen = ZERO
        disputes = self.connection.execute(
            "SELECT * FROM capital_disputes WHERE commitment_id=? AND state='open'",
            (commitment["commitment_id"],),
        ).fetchall()
        for dispute in disputes:
            if dispute["funding_window_id"] is None:
                commitment_frozen += money(dispute["amount"])
            else:
                key = int(dispute["funding_window_id"])
                window_frozen[key] = window_frozen.get(key, ZERO) + money(dispute["amount"])
        standings = evaluate_windows(windows, blockers, contributed, window_frozen, as_of)
        return standings, quantize_money(commitment_frozen), quantize_money(sum(window_frozen.values(), ZERO))

    @staticmethod
    def _effective_caps(standings: Sequence[Any], commitment_frozen: Decimal) -> dict[str, Decimal]:
        """承诺级争议冻结按窗口先后顺序占用可调用额度，返回每窗口实际可调用上限。"""
        remaining_freeze = quantize_money(commitment_frozen)
        caps: dict[str, Decimal] = {}
        for standing in standings:
            cap = standing.callable_cap
            if remaining_freeze > ZERO:
                applied = quantize_money(min(cap, remaining_freeze))
                cap -= applied
                remaining_freeze -= applied
            caps[standing.window_code] = quantize_money(max(ZERO, cap))
        return caps

    def commitment_status(
        self, actor_id: str, project_id: str, party_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        as_of = as_of or self._now()[:10]
        commitment = self._commitment(revision["revision_id"], party_id)
        standings, commitment_frozen, window_frozen_total = self._standings(
            revision["revision_id"], commitment, as_of
        )
        caps = self._effective_caps(standings, commitment_frozen)
        totals = self._party_totals(project_id, party_id)
        open_disputes = [
            dict(row) for row in self.connection.execute(
                "SELECT dispute_id,funding_window_id,amount,reason,raised_at FROM capital_disputes "
                "WHERE commitment_id=? AND state='open' ORDER BY dispute_id",
                (commitment["commitment_id"],),
            )
        ]
        window_codes = {
            int(row["funding_window_id"]): row["window_code"]
            for row in self.connection.execute(
                "SELECT funding_window_id,window_code FROM funding_windows WHERE commitment_id=?",
                (commitment["commitment_id"],),
            )
        }
        for row in open_disputes:
            row["window_code"] = None if row["funding_window_id"] is None else window_codes.get(row["funding_window_id"])
        return {
            "project_id": project_id,
            "revision": revision["revision"],
            "party_id": party_id,
            "party_kind": commitment["party_kind"],
            "committed_amount": commitment["committed_amount"],
            "commitment_state": commitment["state"],
            "as_of": as_of,
            "funding_windows": [
                {**standing.as_dict(), "effective_callable_cap": decimal_text(caps[standing.window_code])}
                for standing in standings
            ],
            "dispute_frozen": {
                "window_specific": decimal_text(window_frozen_total),
                "commitment_level": decimal_text(commitment_frozen),
                "total": decimal_text(quantize_money(window_frozen_total + commitment_frozen)),
            },
            "totals": totals,
            "open_disputes": open_disputes,
        }

    # ---------- 资本调用（未满足条件的金额不得调用） ----------

    def issue_capital_call(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "call.write")
        call_id = str(raw.get("call_id", "")).strip()
        project_id = str(raw.get("project_id", "")).strip()
        due_on = raw.get("due_on")
        if not call_id:
            raise ValidationFailed("call_id 不能为空")
        if not isinstance(due_on, str) or len(due_on.strip()) != 10:
            raise ValidationFailed("due_on 必须是 YYYY-MM-DD 日期")
        due_on = due_on.strip()
        requested = raw.get("items")
        if not isinstance(requested, list) or not requested:
            raise ValidationFailed("items 必须是非空数组")
        idempotency_key = str(raw.get("idempotency_key", "")).strip()
        if not idempotency_key:
            raise ValidationFailed("idempotency_key 不能为空")
        replay = self._idempotent("capital_call", idempotency_key, raw)
        if replay is not None:
            return replay
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        use_category = raw.get("use_category")
        items_out: list[dict[str, Any]] = []
        response: dict[str, Any]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO capital_calls(call_id,project_id,revision_id,use_category,due_on,idempotency_key,"
                "issued_by,issued_at,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    call_id, project_id, revision["revision_id"], use_category, due_on,
                    idempotency_key, actor_id, self._now(), raw.get("note"),
                ),
            )
            for raw_item in requested:
                items_out.append(self._create_call_item(call_id, revision, raw_item, due_on))
            if all(item["state"] == "blocked" for item in items_out):
                call_state = "cancelled"
            elif any(item["state"] == "partial" for item in items_out):
                call_state = "partial"
            else:
                call_state = "issued"
            self.connection.execute(
                "UPDATE capital_calls SET state=? WHERE call_id=?", (call_state, call_id)
            )
            response = {
                "call_id": call_id,
                "project_id": project_id,
                "revision": revision["revision"],
                "due_on": due_on,
                "use_category": use_category,
                "state": call_state,
                "items": items_out,
            }
            self._store_idempotent("capital_call", idempotency_key, raw, response)
            self._audit(
                "capital_call", call_id, "capital_call.issued", actor_id,
                {"project_id": project_id, "revision": revision["revision"], "items": items_out},
            )
        return response

    def _create_call_item(
        self, call_id: str, revision: sqlite3.Row, raw_item: Mapping[str, Any], due_on: str
    ) -> dict[str, Any]:
        party_id = str(raw_item.get("party_id", "")).strip()
        window_code = raw_item.get("window_code")
        if not isinstance(window_code, str) or not window_code.strip():
            raise ValidationFailed("调用条目必须指定 window_code")
        window_code = window_code.strip()
        requested_amount = money(raw_item.get("amount"))
        if requested_amount <= ZERO:
            raise ValidationFailed("调用金额必须大于零")
        commitment = self._commitment(revision["revision_id"], party_id)
        if commitment["state"] == "withdrawn":
            raise InvalidState(f"参与方 {party_id} 已退出该轮承诺")
        standings, commitment_frozen, _ = self._standings(revision["revision_id"], commitment, due_on)
        caps = self._effective_caps(standings, commitment_frozen)
        target_window = next((item for item in standings if item.window_code == window_code), None)
        if target_window is None:
            raise NotFound("出资窗口不存在")
        allowed = min(requested_amount, caps[window_code])
        reasons = self._block_reasons(standings, commitment_frozen, window_code)
        allowed = quantize_money(allowed)
        blocked = quantize_money(requested_amount - allowed)
        if allowed == ZERO:
            state = "blocked"
        elif blocked == ZERO:
            state = "callable"
        else:
            state = "partial"
        window_row = self.connection.execute(
            "SELECT funding_window_id FROM funding_windows WHERE commitment_id=? AND window_code=?",
            (commitment["commitment_id"], window_code),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO capital_call_items(call_id,commitment_id,funding_window_id,requested_amount,"
            "callable_amount,blocked_amount,state,block_reasons_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                call_id, commitment["commitment_id"], window_row["funding_window_id"],
                decimal_text(requested_amount), decimal_text(allowed), decimal_text(blocked),
                state, canonical_json(reasons),
            ),
        )
        return {
            "party_id": party_id,
            "window_code": window_code,
            "requested_amount": decimal_text(requested_amount),
            "callable_amount": decimal_text(allowed),
            "blocked_amount": decimal_text(blocked),
            "state": state,
            "block_reasons": reasons,
        }

    @staticmethod
    def _block_reasons(
        standings: Sequence[Any],
        commitment_frozen: Decimal,
        window_code: str,
    ) -> list[str]:
        reasons: list[str] = []
        standing = next(item for item in standings if item.window_code == window_code)
        if not standing.open_now:
            reasons.append(f"window:{window_code}:closed")
        reasons.extend(standing.gate_reasons)
        if standing.window_frozen > ZERO:
            reasons.append(f"dispute:window:{window_code}:frozen")
        if commitment_frozen > ZERO:
            reasons.append("dispute:commitment:frozen")
        return reasons

    # ---------- 现金流（投入与返还，含依据） ----------

    def record_cash_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "cash.write")
        event = CashEventInput.from_dict(raw)
        replay = self._idempotent("cash_event", event.idempotency_key, raw)
        if replay is not None:
            return replay
        project_id = str(raw.get("project_id", "")).strip()
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        commitment = self._commitment(revision["revision_id"], event.party_id)
        call_item_id: int | None = None
        basis: dict[str, Any]
        if event.direction == "contribution":
            call_item_id, basis = self._contribution_basis(
                raw.get("call_id"), revision, commitment, event
            )
        else:
            call_item_id, basis = self._return_basis(project_id, revision, commitment, event)
        if event.use_category is not None:
            basis["use_category_check"] = self._use_category_check(commitment, event.use_category)
        response: dict[str, Any] = {}
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO cash_events(event_id,project_id,revision_id,commitment_id,call_item_id,"
                    "funding_window_id,party_id,direction,return_kind,amount,occurred_on,use_category,"
                    "basis_json,idempotency_key,recorded_by,created_at,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id, project_id, revision["revision_id"], commitment["commitment_id"],
                        call_item_id, basis.get("funding_window_id"), event.party_id, event.direction,
                        event.return_kind, decimal_text(event.amount), event.occurred_on,
                        event.use_category, canonical_json(basis), event.idempotency_key,
                        actor_id, self._now(), event.note,
                    ),
                )
                event_row_id = int(cursor.lastrowid)
                if call_item_id is not None:
                    self._settle_call_item(call_item_id, event.amount)
                self._store_idempotent("cash_event", event.idempotency_key, raw, {"event_id": event.event_id})
                self._audit(
                    "cash_event", event.event_id, f"cash.{event.direction}.booked", actor_id,
                    {"project_id": project_id, "party_id": event.party_id,
                     "amount": decimal_text(event.amount), "basis": basis},
                )
                response = {
                    "event_id": event.event_id,
                    "project_id": project_id,
                    "party_id": event.party_id,
                    "direction": event.direction,
                    "return_kind": event.return_kind,
                    "amount": decimal_text(event.amount),
                    "occurred_on": event.occurred_on,
                    "state": "booked",
                    "basis": basis,
                    "cash_event_row_id": event_row_id,
                }
        except sqlite3.IntegrityError as exc:
            raise Conflict("现金流编号或幂等键冲突") from exc
        return response

    def _contribution_basis(
        self,
        call_id: object,
        revision: sqlite3.Row,
        commitment: sqlite3.Row,
        event: CashEventInput,
    ) -> tuple[int | None, dict[str, Any]]:
        window_row = self.connection.execute(
            "SELECT * FROM funding_windows WHERE commitment_id=? AND window_code=?",
            (commitment["commitment_id"], event.window_id),
        ).fetchone()
        if window_row is None:
            raise NotFound("出资窗口不存在")
        conditions = [
            {"condition_code": row["condition_code"], "status": row["status"]}
            for row in self.connection.execute(
                "SELECT c.condition_code,c.status FROM commitment_conditions c "
                "JOIN condition_blocked_windows b ON b.condition_id=c.condition_id "
                "WHERE b.funding_window_id=? ORDER BY c.condition_code",
                (window_row["funding_window_id"],),
            )
        ]
        unsatisfied = [item for item in conditions if item["status"] in {"pending", "failed"}]
        if unsatisfied:
            raise InvalidState(f"窗口 {event.window_id} 存在未满足的先决条件，对应资金不得调用")
        opens_on = window_row["opens_on"]
        closes_on = window_row["closes_on"]
        if event.occurred_on < opens_on or (closes_on is not None and event.occurred_on > closes_on):
            raise InvalidState("出资日期不在出资窗口内")
        standings, commitment_frozen, _ = self._standings(
            revision["revision_id"], commitment, event.occurred_on
        )
        caps = self._effective_caps(standings, commitment_frozen)
        effective_cap = caps[event.window_id]
        if effective_cap <= ZERO:
            raise InvalidState(f"窗口 {event.window_id} 可调用额度已被争议全部冻结")
        call_item_id: int | None = None
        explicit_call = call_id is not None
        item: sqlite3.Row | None
        if explicit_call:
            item = self.connection.execute(
                "SELECT i.* FROM capital_call_items i JOIN capital_calls c ON c.call_id=i.call_id "
                "WHERE i.call_id=? AND i.commitment_id=? AND i.funding_window_id=?",
                (str(call_id), commitment["commitment_id"], window_row["funding_window_id"]),
            ).fetchone()
            if item is None:
                raise NotFound("资本调用中不存在该参与方窗口条目")
        else:
            # 未指定调用单时，自动核销该窗口最早的未结清调用条目。
            item = self.connection.execute(
                "SELECT i.* FROM capital_call_items i JOIN capital_calls c ON c.call_id=i.call_id "
                "WHERE i.commitment_id=? AND i.funding_window_id=? AND c.state IN ('issued','partial') "
                "AND i.state IN ('callable','partial') ORDER BY c.due_on,c.issued_at,i.call_item_id LIMIT 1",
                (commitment["commitment_id"], window_row["funding_window_id"]),
            ).fetchone()
        if item is not None:
            if item["state"] == "frozen":
                raise InvalidState("该调用条目处于争议冻结状态")
            outstanding = (
                money(item["callable_amount"])
                - money(item["received_amount"])
                - money(item["frozen_amount"])
            )
            if outstanding <= ZERO:
                raise InvalidState("该调用条目可调用部分已被争议全部冻结")
            if event.amount > outstanding:
                raise Conflict(f"出资额超过调用条目待收金额 {decimal_text(outstanding)}")
            if event.amount > min(outstanding, effective_cap):
                raise Conflict("出资额超过窗口当前可调用额度（可能被争议部分冻结）")
            call_item_id = int(item["call_item_id"])
            call_basis = {
                "call_id": item["call_id"],
                "explicit": explicit_call,
                "callable_amount": item["callable_amount"],
                "outstanding_before": decimal_text(outstanding),
            }
        else:
            if explicit_call:
                raise NotFound("资本调用中不存在可核销的参与方窗口条目")
            if event.amount > effective_cap:
                raise Conflict(
                    f"出资额超过窗口可调用上限 {decimal_text(effective_cap)}（含争议冻结）"
                )
            call_basis = {"call_id": None, "explicit": False,
                          "effective_callable_cap": decimal_text(effective_cap)}
        basis = {
            "funding_window_id": int(window_row["funding_window_id"]),
            "window_code": event.window_id,
            "window_opens_on": opens_on,
            "window_closes_on": closes_on,
            "conditions": conditions,
            "call": call_basis,
            "explanation": "窗口开放、先决条件均已满足/豁免且无争议冻结，出资有效",
        }
        return call_item_id, basis

    def _return_basis(
        self,
        project_id: str,
        revision: sqlite3.Row,
        commitment: sqlite3.Row,
        event: CashEventInput,
    ) -> tuple[int | None, dict[str, Any]]:
        net_rows = self.connection.execute(
            "SELECT w.window_code,w.order_index,"
            "COALESCE(SUM(CASE WHEN e.direction='contribution' THEN CAST(e.amount AS REAL) ELSE 0 END),0) AS contrib,"
            "COALESCE(SUM(CASE WHEN e.direction='return' THEN CAST(e.amount AS REAL) ELSE 0 END),0) AS returned "
            "FROM funding_windows w LEFT JOIN cash_events e ON e.funding_window_id=w.funding_window_id AND e.state='booked' "
            "WHERE w.commitment_id=? GROUP BY w.funding_window_id ORDER BY w.order_index",
            (commitment["commitment_id"],),
        ).fetchall()
        window_net = [
            (row["window_code"], money(row["contrib"]) - money(row["returned"]))
            for row in net_rows
        ]
        total_net = quantize_money(sum((item[1] for item in window_net), ZERO))
        if event.amount > total_net:
            raise Conflict(f"返还金额 {decimal_text(event.amount)} 超过净投入 {decimal_text(total_net)}")
        try:
            allocation = allocate_return(window_net, event.amount)
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        basis = {
            "funding_window_id": None,
            "return_kind": event.return_kind,
            "net_contributions_before": decimal_text(total_net),
            "allocated_to_windows": allocation,
            "explanation": "返还按出资窗口先后顺序冲减各窗口净投入，不超过参与方净投入总额",
        }
        return None, basis

    def _use_category_check(self, commitment: sqlite3.Row, use_category: str) -> dict[str, Any]:
        restrictions = self.connection.execute(
            "SELECT * FROM use_restrictions WHERE commitment_id=? AND restriction_type='use'",
            (commitment["commitment_id"],),
        ).fetchall()
        allowed = True
        messages: list[str] = []
        for restriction in restrictions:
            categories = json.loads(restriction["allowed_categories_json"])
            if categories and use_category not in categories:
                allowed = False
                messages.append(f"违反用途限制 {restriction['restriction_code']}")
        if not allowed:
            raise InvalidState("；".join(messages))
        return {"category": use_category, "allowed": True, "restrictions_checked": len(restrictions)}

    @staticmethod
    def _item_state(callable_amount: Decimal, received: Decimal, frozen: Decimal) -> str:
        receivable = callable_amount - received
        if receivable <= ZERO:
            return "settled"
        if frozen >= receivable:
            return "frozen"
        if received > ZERO:
            return "partial"
        return "callable"

    def _settle_call_item(self, call_item_id: int, amount: Decimal) -> None:
        item = self.connection.execute(
            "SELECT * FROM capital_call_items WHERE call_item_id=?", (call_item_id,)
        ).fetchone()
        callable_amount = money(item["callable_amount"])
        received = money(item["received_amount"]) + money(amount)
        frozen = money(item["frozen_amount"])
        new_state = self._item_state(callable_amount, received, frozen)
        self.connection.execute(
            "UPDATE capital_call_items SET received_amount=?,state=? WHERE call_item_id=?",
            (decimal_text(received), new_state, call_item_id),
        )
        call = self.connection.execute(
            "SELECT call_id FROM capital_call_items WHERE call_item_id=?", (call_item_id,)
        ).fetchone()
        items = self.connection.execute(
            "SELECT state FROM capital_call_items WHERE call_id=?", (call["call_id"],)
        ).fetchall()
        if all(row["state"] in {"settled", "blocked", "frozen"} for row in items):
            call_state = "settled" if any(row["state"] == "settled" for row in items) else "cancelled"
        else:
            call_state = "partial"
        self.connection.execute(
            "UPDATE capital_calls SET state=? WHERE call_id=?", (call_state, call["call_id"])
        )

    def reverse_cash_event(self, actor_id: str, event_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "cash.write")
        row = self.connection.execute(
            "SELECT * FROM cash_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("现金流事件不存在")
        if row["state"] != "booked":
            raise InvalidState("现金流事件已经冲销")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE cash_events SET state='reversed' WHERE event_id=?", (event_id,)
            )
            if row["call_item_id"] is not None:
                item = self.connection.execute(
                    "SELECT * FROM capital_call_items WHERE call_item_id=?", (row["call_item_id"],)
                ).fetchone()
                received = money(item["received_amount"]) - money(row["amount"])
                state = self._item_state(
                    money(item["callable_amount"]), max(ZERO, received), money(item["frozen_amount"])
                )
                self.connection.execute(
                    "UPDATE capital_call_items SET received_amount=?,state=? WHERE call_item_id=?",
                    (decimal_text(max(ZERO, received)), state, row["call_item_id"]),
                )
            self._audit(
                "cash_event", event_id, "cash.reversed", actor_id, {"reason": reason}
            )
        return {"event_id": event_id, "state": "reversed"}

    # ---------- 争议：只冻结相关承诺和金额 ----------

    def raise_dispute(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "dispute.write")
        spec = DisputeSpec.from_dict(raw)
        project_id = str(raw.get("project_id", "")).strip()
        revision = self._current_revision(project_id)
        if revision is None:
            raise NotFound("项目尚无有效协议版本")
        commitment = self._commitment(revision["revision_id"], spec.party_id)
        if user["role"] == "party" and user["party_id"] != spec.party_id:
            raise Forbidden("参与方只能对本机构承诺提出争议")
        funding_window_id: int | None = None
        if spec.window_id is not None:
            window = self.connection.execute(
                "SELECT * FROM funding_windows WHERE commitment_id=? AND window_code=?",
                (commitment["commitment_id"], spec.window_id),
            ).fetchone()
            if window is None:
                raise NotFound("出资窗口不存在")
            funding_window_id = int(window["funding_window_id"])
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO capital_disputes(dispute_id,project_id,revision_id,commitment_id,party_id,"
                    "funding_window_id,amount,reason,raised_by,raised_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.dispute_id, project_id, revision["revision_id"], commitment["commitment_id"],
                        spec.party_id, funding_window_id, decimal_text(spec.amount), spec.reason,
                        actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("争议编号已经存在") from exc
            frozen_items = self._freeze_call_items(
                commitment, funding_window_id, spec.amount, spec.dispute_id
            )
            self._audit(
                "dispute", spec.dispute_id, "dispute.raised", actor_id,
                {"project_id": project_id, "party_id": spec.party_id,
                 "window_code": spec.window_id, "amount": decimal_text(spec.amount),
                 "frozen_call_items": frozen_items},
            )
        return {
            "dispute_id": spec.dispute_id,
            "project_id": project_id,
            "party_id": spec.party_id,
            "window_code": spec.window_id,
            "amount": decimal_text(spec.amount),
            "state": "open",
            "frozen_call_items": frozen_items,
            "unaffected_note": "争议仅冻结相关承诺/窗口金额，其他参与方与窗口继续执行",
        }

    def _freeze_call_items(
        self,
        commitment: sqlite3.Row,
        funding_window_id: int | None,
        amount: Decimal,
        dispute_id: str,
    ) -> list[dict[str, Any]]:
        """按金额冻结相关承诺/窗口下未结清调用条目；只动相关条目，其他继续执行。"""
        remaining = quantize_money(amount)
        frozen: list[dict[str, Any]] = []
        if remaining <= ZERO:
            return frozen
        if funding_window_id is None:
            rows = self.connection.execute(
                "SELECT i.*,w.window_code,w.order_index FROM capital_call_items i "
                "JOIN capital_calls c ON c.call_id=i.call_id JOIN funding_windows w ON w.funding_window_id=i.funding_window_id "
                "WHERE i.commitment_id=? AND c.state IN ('issued','partial') AND i.state IN ('callable','partial','frozen') "
                "ORDER BY w.order_index,i.call_item_id",
                (commitment["commitment_id"],),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT i.*,w.window_code FROM capital_call_items i "
                "JOIN capital_calls c ON c.call_id=i.call_id JOIN funding_windows w ON w.funding_window_id=i.funding_window_id "
                "WHERE i.commitment_id=? AND i.funding_window_id=? AND c.state IN ('issued','partial') "
                "AND i.state IN ('callable','partial','frozen') ORDER BY w.order_index,i.call_item_id",
                (commitment["commitment_id"], funding_window_id),
            ).fetchall()
        for row in rows:
            outstanding = (
                money(row["callable_amount"]) - money(row["received_amount"]) - money(row["frozen_amount"])
            )
            if outstanding <= ZERO or remaining <= ZERO:
                continue
            take = quantize_money(min(outstanding, remaining))
            new_frozen = money(row["frozen_amount"]) + take
            receivable = money(row["callable_amount"]) - money(row["received_amount"])
            received = money(row["received_amount"])
            if receivable <= ZERO:
                new_state = "settled"
            elif new_frozen >= receivable:
                new_state = "frozen"
            elif received > ZERO:
                new_state = "partial"
            else:
                new_state = "callable"
            self.connection.execute(
                "UPDATE capital_call_items SET frozen_amount=?,state=? WHERE call_item_id=?",
                (decimal_text(new_frozen), new_state, row["call_item_id"]),
            )
            self.connection.execute(
                "INSERT INTO dispute_freezes(dispute_id,call_item_id,frozen_amount) VALUES(?,?,?)",
                (dispute_id, row["call_item_id"], decimal_text(take)),
            )
            frozen.append({"call_id": row["call_id"], "window_code": row["window_code"],
                           "frozen_amount": decimal_text(take), "item_state": new_state})
            remaining -= take
        return frozen

    def resolve_dispute(
        self, actor_id: str, dispute_id: str, resolution: str, *, cancelled: bool = False
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.resolve")
        dispute = self.connection.execute(
            "SELECT * FROM capital_disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if dispute is None:
            raise NotFound("争议不存在")
        if dispute["state"] != "open":
            raise InvalidState("争议已经处理")
        new_state = "cancelled" if cancelled else "resolved"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE capital_disputes SET state=?,resolved_at=?,resolution=? WHERE dispute_id=?",
                (new_state, self._now(), resolution, dispute_id),
            )
            restored: list[dict[str, Any]] = []
            if not cancelled:
                restored = self._restore_call_items(dispute)
            self._audit(
                "dispute", dispute_id, f"dispute.{new_state}", actor_id,
                {"resolution": resolution, "restored_call_items": restored},
            )
        return {"dispute_id": dispute_id, "state": new_state, "restored_call_items": restored}

    def _restore_call_items(self, dispute: sqlite3.Row) -> list[dict[str, Any]]:
        """争议解除：仅释放本争议冻结的份额，再按剩余冻结/已收金额重算条目状态。"""
        freezes = self.connection.execute(
            "SELECT * FROM dispute_freezes WHERE dispute_id=?", (dispute["dispute_id"],)
        ).fetchall()
        restored: list[dict[str, Any]] = []
        for freeze in freezes:
            item = self.connection.execute(
                "SELECT i.*,w.window_code FROM capital_call_items i "
                "JOIN funding_windows w ON w.funding_window_id=i.funding_window_id "
                "WHERE i.call_item_id=?",
                (freeze["call_item_id"],),
            ).fetchone()
            if item is None:
                continue
            remaining_frozen = money(item["frozen_amount"]) - money(freeze["frozen_amount"])
            received = money(item["received_amount"])
            receivable = money(item["callable_amount"]) - received
            if receivable <= ZERO:
                new_state = "settled"
            elif remaining_frozen >= receivable:
                new_state = "frozen"
            elif received > ZERO:
                new_state = "partial"
            else:
                new_state = "callable"
            self.connection.execute(
                "UPDATE capital_call_items SET frozen_amount=?,state=? WHERE call_item_id=?",
                (decimal_text(max(ZERO, remaining_frozen)), new_state, item["call_item_id"]),
            )
            restored.append({
                "call_id": item["call_id"], "window_code": item["window_code"],
                "released_amount": freeze["frozen_amount"], "state": new_state,
            })
        self.connection.execute(
            "DELETE FROM dispute_freezes WHERE dispute_id=?", (dispute["dispute_id"],)
        )
        return restored

    # ---------- 周期核算（从有效事件重算）与依据解释 ----------

    def _party_totals(self, project_id: str, party_id: str) -> dict[str, object]:
        events = list(self.connection.execute(
            "SELECT event_id,direction,return_kind,party_id,amount,occurred_on FROM cash_events "
            "WHERE project_id=? AND party_id=? AND state='booked' ORDER BY occurred_on,event_id",
            (project_id, party_id),
        ))
        return period_totals(dict(row) for row in events)

    def project_ledger(
        self,
        actor_id: str,
        project_id: str,
        *,
        start_on: str | None = None,
        end_on: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._project(project_id)
        clauses = ["project_id=?", "state='booked'"]
        params: list[object] = [project_id]
        if start_on is not None:
            clauses.append("occurred_on>=?")
            params.append(start_on)
        if end_on is not None:
            clauses.append("occurred_on<=?")
            params.append(end_on)
        where = " AND ".join(clauses)
        rows = [
            dict(row) for row in self.connection.execute(
                f"SELECT event_id,direction,return_kind,party_id,amount,occurred_on "
                f"FROM cash_events WHERE {where} ORDER BY occurred_on,event_id",
                params,
            )
        ]
        per_party: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            per_party.setdefault(str(row["party_id"]), []).append(row)
        parties = []
        total_contributions = ZERO
        total_returns = ZERO
        for party_id in sorted(per_party):
            totals = period_totals(per_party[party_id])
            total_contributions += money(totals["contributions"])
            total_returns += money(totals["returns"])
            parties.append({"party_id": party_id, **totals})
        overall = period_totals(rows)
        return {
            "project_id": project_id,
            "period": {"start_on": start_on, "end_on": end_on},
            "recomputed_from": "cash_events(state='booked')",
            "contributions": decimal_text(total_contributions),
            "returns": decimal_text(total_returns),
            "net_position": decimal_text(quantize_money(total_contributions - total_returns)),
            "event_count": overall["event_count"],
            "parties": parties,
        }

    def cash_events(
        self, actor_id: str, project_id: str, party_id: str | None = None
    ) -> dict[str, Any]:
        """逐笔列出投入与返还及其依据，供接口解释资金从何而来、为何返还。"""
        self._require(actor_id, "report.read")
        clauses = ["project_id=?"]
        params: list[object] = [project_id]
        if party_id is not None:
            clauses.append("party_id=?")
            params.append(party_id)
        rows = self.connection.execute(
            f"SELECT event_id,revision_id,party_id,direction,return_kind,amount,occurred_on,"
            f"use_category,state,basis_json,recorded_by,created_at FROM cash_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY occurred_on,event_id",
            params,
        ).fetchall()
        events = []
        for row in rows:
            events.append({
                "event_id": row["event_id"],
                "revision_id": row["revision_id"],
                "party_id": row["party_id"],
                "direction": row["direction"],
                "return_kind": row["return_kind"],
                "amount": row["amount"],
                "occurred_on": row["occurred_on"],
                "use_category": row["use_category"],
                "state": row["state"],
                "basis": json.loads(row["basis_json"]),
                "recorded_by": row["recorded_by"],
                "created_at": row["created_at"],
            })
        return {"project_id": project_id, "events": events}

    # ---------- 重启恢复未决冲突 ----------

    def recover_pending(self, actor_id: str, project_id: str | None = None) -> dict[str, Any]:
        """进程重启后恢复尚未解决的承诺冲突、未确认部分与未结清调用。"""
        self._require(actor_id, "report.read")
        if project_id is not None:
            self._project(project_id)
        scope_filter = "" if project_id is None else " AND d.project_id=?"
        params: list[object] = [] if project_id is None else [project_id]
        disputes = [
            dict(row) for row in self.connection.execute(
                "SELECT d.dispute_id,d.project_id,d.revision_id,d.party_id,d.funding_window_id,"
                "d.amount,d.reason,d.raised_at,w.window_code FROM capital_disputes d "
                "LEFT JOIN funding_windows w ON w.funding_window_id=d.funding_window_id "
                "WHERE d.state='open'" + scope_filter + " "
                "ORDER BY d.raised_at,d.dispute_id",
                params,
            )
        ]
        frozen_items = [
            dict(row) for row in self.connection.execute(
                "SELECT c.call_id,c.project_id,p.party_id,w.window_code,i.callable_amount,"
                "i.received_amount,i.frozen_amount FROM capital_call_items i "
                "JOIN capital_calls c ON c.call_id=i.call_id "
                "JOIN party_commitments p ON p.commitment_id=i.commitment_id "
                "JOIN funding_windows w ON w.funding_window_id=i.funding_window_id "
                "WHERE i.frozen_amount>0"
                + (" AND c.project_id=?" if project_id is not None else ""),
                params,
            )
        ]
        open_calls = [
            dict(row) for row in self.connection.execute(
                "SELECT call_id,project_id,state,due_on FROM capital_calls "
                "WHERE state IN ('issued','partial')"
                + (" AND project_id=?" if project_id is not None else ""),
                params,
            )
        ]
        unconfirmed = [
            dict(row) for row in self.connection.execute(
                "SELECT r.project_id,r.revision,c.party_id,c.state FROM party_commitments c "
                "JOIN agreement_revisions r ON r.revision_id=c.revision_id "
                "WHERE r.state='effective' AND c.state='proposed'"
                + (" AND r.project_id=?" if project_id is not None else "")
                + " ORDER BY r.project_id,c.party_id",
                params,
            )
        ]
        return {
            "recovered_at": self._now(),
            "open_disputes": disputes,
            "frozen_call_items": frozen_items,
            "open_calls": open_calls,
            "unconfirmed_commitments": unconfirmed,
            "conflict_count": len(disputes),
        }

    # ---------- 审计链 ----------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM capital_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
