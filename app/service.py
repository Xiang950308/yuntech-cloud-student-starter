#!/usr/bin/env python3
"""Inspection event service."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def make_server(version_file, port=8080, auth_file="/etc/inspection/app.env"):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    tokens = {"reporter": "", "operator": ""}
    try:
        for line in Path(auth_file).read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in ("REPORTER_TOKEN", "OPERATOR_TOKEN"):
                tokens["reporter" if key.startswith("REPORTER") else "operator"] = value
    except FileNotFoundError:
        pass
    auth_configured = all(tokens.values())
    events = {}

    def role_for(handler):
        scheme, separator, supplied = handler.headers.get("Authorization", "").partition(" ")
        if separator != " " or scheme != "Bearer" or not supplied:
            return None
        for role, token in tokens.items():
            if token and hmac.compare_digest(supplied, token):
                return role
        return None

    def json_response(handler, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)

    def error(handler, status, reason, field=None):
        body = {"error": reason}
        if field:
            body["field"] = field
        json_response(handler, status, body)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                json_response(self, 200, {"status": "ok", "service": "inspection", "version": version,
                                          "started_at": started, "auth_configured": auth_configured})
                return
            if path == "/":
                html = """<!doctype html><html><head><meta charset="utf-8"><title>Inspection events</title></head>
<body><h1>Inspection events</h1><label>Operator token <input id="token" type="password" autocomplete="off"></label>
<button id="load" type="button">Load</button><p id="error"></p><pre id="events"></pre>
<script>
const token = document.getElementById('token');
const output = document.getElementById('events');
document.getElementById('load').onclick = async () => {
  output.textContent = '';
  document.getElementById('error').textContent = '';
  try {
    const response = await fetch('/events', {headers: {Authorization: 'Bearer ' + token.value}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'request failed');
    output.textContent = JSON.stringify(result, null, 2);
    token.value = '';
  } catch (err) { document.getElementById('error').textContent = err.message; }
};
</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return
            if path == "/events":
                role = role_for(self)
                if role != "operator":
                    error(self, 401 if role is None else 403, "unauthorized" if role is None else "forbidden")
                else:
                    json_response(self, 200, list(events.values())[-50:][::-1])
                return
            match = re.fullmatch(r"/events/([^/]+)", path)
            if match:
                role = role_for(self)
                if role != "operator":
                    error(self, 401 if role is None else 403, "unauthorized" if role is None else "forbidden")
                elif match.group(1) not in events:
                    error(self, 404, "not_found")
                else:
                    json_response(self, 200, events[match.group(1)])
                return
            error(self, 404, "not_found")

        def do_POST(self):
            if urlsplit(self.path).path != "/events":
                error(self, 404, "not_found")
                return
            role = role_for(self)
            if role is None:
                error(self, 401, "unauthorized")
                return
            if role != "reporter":
                error(self, 403, "forbidden")
                return
            if self.headers.get_content_type() != "application/json":
                error(self, 400, "content_type", "Content-Type")
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > 4096:
                error(self, 400, "body_too_large")
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                error(self, 400, "invalid_json")
                return
            if not isinstance(payload, dict):
                error(self, 400, "object_required")
                return
            allowed = {"event_id", "device_id", "observed_at", "type", "note"}
            extra = set(payload) - allowed
            if extra:
                error(self, 400, "unknown_field", sorted(extra)[0])
                return
            for field, maximum in (("event_id", 64), ("device_id", 32)):
                value = payload.get(field)
                if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    error(self, 400, "invalid_value", field)
                    return
            observed_at = payload.get("observed_at")
            if not isinstance(observed_at, str) or not re.fullmatch(r".+(?:Z|[+-]\d{2}:\d{2})", observed_at):
                error(self, 400, "invalid_value", "observed_at")
                return
            try:
                datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError:
                error(self, 400, "invalid_value", "observed_at")
                return
            if payload.get("type") not in {"status", "anomaly", "test"}:
                error(self, 400, "invalid_value", "type")
                return
            if "note" in payload and (not isinstance(payload["note"], str) or len(payload["note"]) > 200):
                error(self, 400, "invalid_value", "note")
                return
            if payload["event_id"] in events:
                error(self, 409, "duplicate", "event_id")
                return
            event = dict(payload)
            event["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            events[event["event_id"]] = event
            json_response(self, 201, event)

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
