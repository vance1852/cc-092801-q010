"""无第三方依赖的联合资本承诺与结算 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import SyndicateError, ValidationFailed
from .service import CapitalSyndicateService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: CapitalSyndicateService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)

        def q(name: str) -> str | None:
            return query.get(name, [None])[0]

        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"], payload.get("party_id")
                ))
            if method == "POST" and path == "/projects":
                return Response(201, service.create_project(actor, payload))
            if method == "POST" and path == "/agreement_revisions":
                return Response(201, service.publish_revision(actor, payload))
            if method == "GET" and len(parts) >= 2 and parts[0] == "projects":
                project_id = parts[1]
                if len(parts) == 2:
                    revision = q("revision")
                    return Response(200, service.agreement(
                        actor, project_id, None if revision is None else int(revision)
                    ))
                if len(parts) == 3 and parts[2] == "ledger":
                    return Response(200, service.project_ledger(
                        actor, project_id, start_on=q("start_on"), end_on=q("end_on")
                    ))
                if len(parts) == 3 and parts[2] == "cash_events":
                    return Response(200, service.cash_events(actor, project_id, q("party_id")))
                if len(parts) == 3 and parts[2] == "recover":
                    return Response(200, service.recover_pending(actor, project_id))
                if len(parts) == 4 and parts[2] == "commitments":
                    return Response(200, service.commitment_status(
                        actor, project_id, parts[3], q("as_of")
                    ))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "confirmations":
                return Response(201, service.confirm_part(
                    actor, parts[1], payload["party_id"], payload["scope"],
                    payload.get("item_ref"), payload.get("note")
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "conditions":
                return Response(200, service.decide_condition(
                    actor, parts[1], payload["party_id"], payload["condition_code"],
                    payload["status"], payload.get("note")
                ))
            if method == "POST" and path == "/capital_calls":
                return Response(201, service.issue_capital_call(actor, payload))
            if method == "POST" and path == "/cash_events":
                return Response(201, service.record_cash_event(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "cash_events" and parts[2] == "reverse":
                return Response(200, service.reverse_cash_event(actor, parts[1], payload["reason"]))
            if method == "POST" and path == "/disputes":
                return Response(201, service.raise_dispute(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, service.resolve_dispute(
                    actor, parts[1], payload["resolution"],
                    cancelled=bool(payload.get("cancelled", False)),
                ))
            if method == "POST" and path == "/recover":
                return Response(200, service.recover_pending(actor, q("project_id")))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except SyndicateError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SyndicateCapital/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动联合资本承诺与结算服务")
    parser.add_argument("--database", type=Path, default=Path("capital_ops.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(CapitalSyndicateService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
