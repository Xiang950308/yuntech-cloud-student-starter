#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
AUTH_FILE="$ROOT/.local/app.env"
RESOURCES="$ROOT/.local/resources.json"
COMMIT=${1:-HEAD}

[[ -f "$AUTH_FILE" ]] || { echo "Missing .local/app.env" >&2; exit 1; }
[[ "$(stat -c '%a' "$AUTH_FILE")" == "600" ]] || { echo ".local/app.env must be mode 600" >&2; exit 1; }
[[ -f "$RESOURCES" ]] || { echo "Missing .local/resources.json" >&2; exit 1; }

readarray -t target_info < <(python3 - "$ROOT" "$COMMIT" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
commit = subprocess.check_output(
    ["git", "rev-parse", "--verify", "--end-of-options", sys.argv[2] + "^{commit}"],
    cwd=root, text=True).strip()
record = json.loads((root / ".local/resources.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(root / "scripts"))
import lab

item = lab.run_aws(["ec2", "describe-instances", "--instance-ids", record["instance_id"]], record["region"])["Reservations"][0]["Instances"][0]
if item["State"]["Name"] != "running" or not item.get("PublicIpAddress"):
    raise SystemExit("recorded instance is not running or has no public IP")
print(commit)
print(item["PublicIpAddress"])
print(record["key_path"])
PY
)
DEPLOY_COMMIT=${target_info[0]}
TARGET_IP=${target_info[1]}
KEY_PATH=${target_info[2]}
USER_DATA="$ROOT/.local/deploy-user-data.$$"
trap 'rm -f "$USER_DATA"' EXIT

bash "$ROOT/deploy/make-user-data.sh" "$COMMIT" "$USER_DATA" >/dev/null
echo "Target: $TARGET_IP"
echo "Commit: $DEPLOY_COMMIT"
read -r -p "Type DEPLOY to continue: " approval
[[ "$approval" == "DEPLOY" ]] || { echo "Deployment cancelled"; exit 1; }

SSH=(ssh -i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ec2-user@$TARGET_IP")
"${SSH[@]}" "sudo bash -s" < "$USER_DATA"
"${SSH[@]}" "sudo install -m 600 /dev/stdin /etc/inspection/app.env" < "$AUTH_FILE"
"${SSH[@]}" "sudo systemctl restart inspection"

python3 - "$TARGET_IP" "$DEPLOY_COMMIT" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(f"http://{sys.argv[1]}/health", timeout=10) as response:
    body = json.load(response)
if response.status != 200 or body.get("version") != sys.argv[2] or body.get("auth_configured") is not True:
    raise SystemExit("health check failed")
print("Health OK: HTTP 200, version matches commit, auth_configured=true")
PY