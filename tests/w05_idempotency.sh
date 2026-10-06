#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RESOURCES="$ROOT/.local/resources.json"
AUTH_FILE="$ROOT/.local/app.env"
[[ -f "$RESOURCES" && -f "$AUTH_FILE" ]] || { echo "Missing .local resources or auth file" >&2; exit 1; }
[[ "$(stat -c '%a' "$AUTH_FILE")" == "600" ]] || { echo ".local/app.env must be mode 600" >&2; exit 1; }

readarray -t target_info < <(python3 - "$ROOT" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads((Path(sys.argv[1]) / ".local/resources.json").read_text(encoding="utf-8"))
print(record["public_ip"])
print(record["key_path"])
PY
)
TARGET_IP=${target_info[0]}
KEY_PATH=${target_info[1]}
REPORTER_TOKEN=$(awk -F= '$1 == "REPORTER_TOKEN" {print substr($0, index($0, "=") + 1)}' "$AUTH_FILE")
[[ -n "$REPORTER_TOKEN" ]] || { echo "Missing reporter token" >&2; exit 1; }

BASE="http://$TARGET_IP"
EVENT_ID="w5-matrix-$(date -u +%Y%m%d%H%M%S)"
BODY=$(printf '{"event_id":"%s","device_id":"matrix-device","observed_at":"2026-10-06T00:00:00Z","type":"test","note":"matrix"}' "$EVENT_ID")
CHANGED=$(printf '{"event_id":"%s","device_id":"matrix-device","observed_at":"2026-10-06T00:00:00Z","type":"test","note":"changed"}' "$EVENT_ID")

health=$(curl --silent --show-error --fail "$BASE/health")
python3 -c 'import json,sys; body=json.loads(sys.argv[1]); print(json.dumps({"version": body.get("version"), "db_configured": body.get("db_configured")}))' "$health"

request() {
    local number=$1 payload=$2 expected=$3 response status body
    response=$(curl --silent --show-error --request POST "$BASE/events" \
        --header "Authorization: Bearer $REPORTER_TOKEN" \
        --header 'Content-Type: application/json' --data "$payload" --write-out $'\n%{http_code}')
    status=${response##*$'\n'}
    body=${response%$'\n'*}
    printf '#%s HTTP %s (expected %s) %s\n' "$number" "$status" "$expected" "$body"
    [[ "$status" == "$expected" ]] || exit 1
}

request 1 "$BODY" 201
request 2 "$BODY" 200
request 3 "$CHANGED" 409
ssh -i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ec2-user@$TARGET_IP" 'sudo systemctl restart inspection'
request 4 "$BODY" 200

count=$(ssh -i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ec2-user@$TARGET_IP" \
    "sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; PGPASSWORD=\"\$DB_PASSWORD\" psql \"host=\$DB_HOST dbname=\$DB_NAME user=\$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem\" -At -v event_id=\"$EVENT_ID\"'" \
    <<< "SELECT count(*) FROM events WHERE event_id = :'event_id';")
printf '#5 psql count for %s: %s (expected 1)\n' "$EVENT_ID" "$count"
[[ "$count" == "1" ]]