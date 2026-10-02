"""无第三方依赖的联合资本承诺与结算 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import CapitalOpsError, InvalidState, ValidationFailed
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
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/parties":
                return Response(201, self.service.register_party(actor, payload))
            if method == "POST" and path == "/rounds/versions":
                return Response(201, self.service.publish_round_version(actor, payload))
            if method == "POST" and path == "/commitments":
                return Response(201, self.service.create_commitment(actor, payload))
            if method == "POST" and path == "/commitments/confirmations":
                return Response(201, self.service.confirm_commitment(actor, payload))
            if method == "POST" and path == "/conditions":
                return Response(201, self.service.attach_condition(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "conditions" and parts[2] == "satisfy":
                return Response(200, self.service.satisfy_condition(actor, parts[1], payload.get("evidence", "")))
            if method == "POST" and path == "/windows":
                return Response(201, self.service.open_window(actor, payload))
            if method == "POST" and path == "/restrictions":
                return Response(201, self.service.impose_restriction(actor, payload))
            if method == "POST" and path == "/follow_ons":
                return Response(201, self.service.grant_follow_on(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "follow_ons" and parts[2] == "exercise":
                return Response(201, self.service.exercise_follow_on(actor, payload))
            if method == "POST" and path == "/capital_calls":
                return Response(201, self.service.call_capital(actor, payload))
            if method == "POST" and path == "/receipts":
                return Response(201, self.service.record_receipt(actor, payload))
            if method == "POST" and path == "/distributions":
                return Response(201, self.service.declare_distribution(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "payments":
                return Response(201, self.service.record_payment(actor, payload))
            if method == "POST" and path == "/disputes":
                return Response(201, self.service.open_dispute(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, self.service.resolve_dispute(actor, payload))
            if method == "POST" and path == "/adjustments":
                return Response(201, self.service.append_adjustment(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "rounds" and parts[2] == "close":
                return Response(200, self.service.close_round(actor, parts[1]))

            if method == "GET" and len(parts) == 3 and parts[0] == "rounds" and parts[2] == "statement":
                as_of = query.get("as_of", [None])[0]
                return Response(200, self.service.round_statement(actor, parts[1], as_of))
            if method == "GET" and len(parts) == 3 and parts[0] == "rounds" and parts[2] == "events":
                return Response(200, self.service.round_events(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "commitments":
                return Response(200, self.service.commitment_status(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "eligibility":
                return Response(
                    200,
                    self.service.eligibility_preview(
                        actor, parts[1], query["window_id"][0], query["amount"][0]
                    ),
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "receipts" and parts[2] == "explain":
                return Response(200, self.service.explain_contribution(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "explain":
                return Response(
                    200,
                    self.service.explain_return(actor, parts[1], query["commitment_id"][0]),
                )
            if method == "GET" and path == "/disputes/unresolved":
                return Response(200, self.service.recover_unresolved_disputes())
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except InvalidState as exc:
            error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.details is not None:
                error["details"] = exc.details
            return Response(exc.status, {"error": error})
        except CapitalOpsError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CapitalSyndicate/1"

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
    parser = argparse.ArgumentParser(description="启动耐心资本联合承诺与结算服务")
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
