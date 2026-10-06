#!/usr/bin/env python3
"""Inspection event service."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


DB_FIELDS = ("event_id", "device_id", "observed_at", "type", "note")


def make_server(version_file, port=8080, auth_file=None):
    auth_file = auth_file or os.environ.get("INSPECTION_AUTH_FILE", "/etc/inspection/app.env")
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
    db = {}
    try:
        for line in Path(auth_file).read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
                db[key] = value
    except FileNotFoundError:
        pass
    db_configured = all(db.get(key) for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"))
    events = {}

    def db_connection():
        try:
            import psycopg2
            return psycopg2.connect(host=db["DB_HOST"], dbname=db["DB_NAME"], user=db["DB_USER"],
                                    password=db["DB_PASSWORD"], sslmode="verify-full",
                                    sslrootcert="/etc/inspection/rds-ca.pem", connect_timeout=5)
        except (ImportError, KeyError):
            raise RuntimeError("database driver is unavailable")

    def ensure_schema(connection):
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id VARCHAR(64) PRIMARY KEY,
                    device_id VARCHAR(32) NOT NULL,
                    observed_at TEXT NOT NULL,
                    type VARCHAR(16) NOT NULL,
                    note TEXT,
                    received_at TIMESTAMPTZ NOT NULL
                )
            """)
        connection.commit()

    def row_to_event(row):
        event = dict(zip(DB_FIELDS + ("received_at",), row))
        received_at = event["received_at"]
        if hasattr(received_at, "isoformat"):
            event["received_at"] = received_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return event

    def event_matches(left, right):
        return all(left.get(field) == right.get(field) for field in DB_FIELDS)

    def database_error(exc):
        detail = str(exc).replace(db.get("DB_HOST", ""), "<db-host>")
        detail = detail.replace(db.get("DB_PASSWORD", ""), "<redacted>")
        print(f"database error: {type(exc).__name__}: {detail}", flush=True)

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
                                          "started_at": started, "auth_configured": auth_configured,
                                          "db_configured": db_configured})
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
                    if not db_configured:
                        json_response(self, 200, list(events.values())[-50:][::-1])
                        return
                    try:
                        with db_connection() as connection:
                            ensure_schema(connection)
                            with connection.cursor() as cursor:
                                cursor.execute("SELECT event_id, device_id, observed_at, type, note, received_at "
                                               "FROM events ORDER BY received_at DESC LIMIT 50")
                                result = [row_to_event(row) for row in cursor.fetchall()]
                        json_response(self, 200, result)
                    except Exception as exc:
                        database_error(exc)
                        error(self, 503, "database_unavailable")
                return
            match = re.fullmatch(r"/events/([^/]+)", path)
            if match:
                role = role_for(self)
                if role != "operator":
                    error(self, 401 if role is None else 403, "unauthorized" if role is None else "forbidden")
                else:
                    if not db_configured:
                        event = events.get(match.group(1))
                    else:
                        try:
                            with db_connection() as connection:
                                ensure_schema(connection)
                                with connection.cursor() as cursor:
                                    cursor.execute("SELECT event_id, device_id, observed_at, type, note, received_at "
                                                   "FROM events WHERE event_id = %s", (match.group(1),))
                                    row = cursor.fetchone()
                                    event = row_to_event(row) if row else None
                        except Exception as exc:
                            database_error(exc)
                            error(self, 503, "database_unavailable")
                            return
                    if event is None:
                        error(self, 404, "not_found")
                    else:
                        json_response(self, 200, event)
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
            event = dict(payload)
            event["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            if not db_configured:
                existing = events.get(event["event_id"])
                if existing is not None:
                    if event_matches(existing, event):
                        json_response(self, 200, existing)
                    else:
                        error(self, 409, "duplicate", "event_id")
                    return
                events[event["event_id"]] = event
                json_response(self, 201, event)
                return
            try:
                with db_connection() as connection:
                    ensure_schema(connection)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO events (event_id, device_id, observed_at, type, note, received_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING "
                            "RETURNING event_id, device_id, observed_at, type, note, received_at",
                            (event["event_id"], event["device_id"], event["observed_at"], event["type"],
                             event.get("note"), event["received_at"]),
                        )
                        row = cursor.fetchone()
                        if row is not None:
                            saved = row_to_event(row)
                            connection.commit()
                            json_response(self, 201, saved)
                            return
                        cursor.execute("SELECT event_id, device_id, observed_at, type, note, received_at "
                                       "FROM events WHERE event_id = %s", (event["event_id"],))
                        existing = row_to_event(cursor.fetchone())
                        connection.commit()
                if event_matches(existing, event):
                    json_response(self, 200, existing)
                else:
                    error(self, 409, "duplicate", "event_id")
            except Exception as exc:
                database_error(exc)
                error(self, 503, "database_unavailable")

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
