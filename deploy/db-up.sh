#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec python3 - "$ROOT" <<'PY'
import ipaddress
import json
import os
from pathlib import Path
import secrets
import string
import sys
import time

root = Path(sys.argv[1])
local = root / ".local"
resources_path = local / "resources.json"
if not resources_path.exists():
    raise SystemExit("Missing .local/resources.json; create or restore the recorded EC2 first.")

sys.path.insert(0, str(root / "scripts"))
import lab

record = json.loads(resources_path.read_text(encoding="utf-8"))
for key in ("region", "vpc_id", "security_group_id", "instance_id"):
    if key not in record:
        raise SystemExit(f"Resource record missing {key}")
resume = bool(record.get("db_instance_identifier"))

region = record["region"]
ctx = lab.verify()
if ctx["region"] != region:
    raise SystemExit("Region does not match the verified Learner Lab context.")

def save():
    resources_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.chmod(resources_path, 0o600)

vpc = lab.run_aws(["ec2", "describe-vpcs", "--vpc-ids", record["vpc_id"]], region)["Vpcs"][0]
vpc_network = ipaddress.ip_network(vpc["CidrBlock"])
subnets = lab.run_aws(["ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={record['vpc_id']}"], region)["Subnets"]
used = [ipaddress.ip_network(item["CidrBlock"]) for item in subnets]
azs = lab.run_aws(["ec2", "describe-availability-zones", "--filters", "Name=state,Values=available"], region)["AvailabilityZones"]
chosen_azs = [item["ZoneName"] for item in azs if item["ZoneName"] != next(
    subnet["AvailabilityZone"] for subnet in subnets if subnet["SubnetId"] == record["subnet_id"])]
if len(chosen_azs) < 2:
    raise SystemExit("Need two available AZs different from the recorded EC2 subnet.")

candidate_blocks = list(vpc_network.subnets(new_prefix=24))
free_blocks = [block for block in candidate_blocks if not any(block.overlaps(existing) for existing in used)]
if len(free_blocks) < 2:
    raise SystemExit("Could not find two non-overlapping /24 blocks in the VPC.")
subnet_blocks = free_blocks[:2]
db_identifier = f"inspection-w5-{ctx['account'][-4:]}".lower()
print(json.dumps({
    "will_create": ["2 private /24 subnets in 2 AZs", "1 local-only route table", "1 DB subnet group",
                    "1 SG-db with only host-SG -> TCP 5432", "1 encrypted private PostgreSQL db.t3.micro"],
    "network": {"vpc": record["vpc_id"], "subnets": [str(block) for block in subnet_blocks], "azs": chosen_azs[:2]},
    "database": {"identifier": db_identifier, "engine": "postgres", "class": "db.t3.micro", "storage_gib": 20,
                 "publicly_accessible": False, "multi_az": False, "database": "inspection"},
    "costs": "RDS db.t3.micro and 20 GiB gp3 while running; subnets, route table and SG have no hourly charge",
    "exposure": "RDS has no public address; SG-db allows TCP 5432 only from the recorded EC2 security group",
    "cleanup": "Stop the recorded EC2 and RDS; retain the private subnets, local-only route table, DB subnet group and SG-db for W6",
}, indent=2))
if os.environ.get("W05_APPROVAL") != "CREATE-W05":
    lab.approve("Review the exact W5 resource, exposure, cost and retention plan above.", "CREATE-W05")

tags = [{"Key": key, "Value": str(value)} for key, value in {
    "course": "yuntech-115-1", "week": "w05", "group": record.get("group", "4"), "owner": record.get("owner", "unknown")
}.items()]
route_table = lab.run_aws(["ec2", "create-route-table", "--vpc-id", record["vpc_id"], "--tag-specifications",
                           json.dumps([{"ResourceType": "route-table", "Tags": tags}])], region)["RouteTable"]["RouteTableId"] \
    if not resume else record["db_route_table_id"]
if not resume:
    record["db_route_table_id"] = route_table
    save()

if not resume:
    subnet_ids = []
    for block, az in zip(subnet_blocks, chosen_azs[:2]):
        subnet = lab.run_aws(["ec2", "create-subnet", "--vpc-id", record["vpc_id"], "--cidr-block", str(block),
                              "--availability-zone", az, "--tag-specifications",
                              json.dumps([{"ResourceType": "subnet", "Tags": tags}])], region)["Subnet"]["SubnetId"]
        subnet_ids.append(subnet)
        lab.run_aws(["ec2", "associate-route-table", "--route-table-id", route_table, "--subnet-id", subnet], region)
    record["db_subnet_ids"] = subnet_ids
    save()
else:
    subnet_ids = record["db_subnet_ids"]

group = record.get("db_subnet_group")
if not resume:
    group = lab.run_aws(["rds", "create-db-subnet-group", "--db-subnet-group-name", db_identifier,
                         "--db-subnet-group-description", "W5 private inspection database", "--subnet-ids", *subnet_ids,
                         "--tags", json.dumps(tags)], region)["DBSubnetGroup"]["DBSubnetGroupName"]
    record["db_subnet_group"] = group
    save()

db_sg = record.get("db_security_group_id")
if not resume:
    db_sg = lab.run_aws(["ec2", "create-security-group", "--group-name", f"{db_identifier}-sg",
                         "--description", "W5 private inspection database SG", "--vpc-id", record["vpc_id"]], region)["GroupId"]
    record["db_security_group_id"] = db_sg
    save()
    lab.run_aws(["ec2", "create-tags", "--resources", db_sg, "--tags", json.dumps(tags)], region)
    lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", db_sg, "--protocol", "tcp",
                 "--port", "5432", "--source-group", record["security_group_id"]], region)

alphabet = string.ascii_letters + string.digits
password = "".join(secrets.choice(alphabet) for _ in range(32))
db_env = local / "db.env"
password_file = local / f".db-password.{os.getpid()}"
password_file.write_text(password, encoding="utf-8")
os.chmod(password_file, 0o600)
try:
    import atexit
    atexit.register(lambda: password_file.unlink(missing_ok=True))
except OSError:
    password_file.unlink(missing_ok=True)
db_env.write_text(f"DB_HOST=\nDB_PORT=5432\nDB_NAME=inspection\nDB_USER=inspection\nDB_PASSWORD={password}\n", encoding="utf-8")
os.chmod(db_env, 0o600)
record["db_instance_identifier"] = db_identifier
save()

lab.run_aws(["rds", "create-db-instance", "--db-instance-identifier", db_identifier, "--engine", "postgres",
             "--db-instance-class", "db.t3.micro", "--allocated-storage", "20", "--storage-type", "gp3",
             "--master-username", "inspection", "--master-user-password", f"file://{password_file}",
             "--db-name", "inspection", "--db-subnet-group-name", group, "--vpc-security-group-ids", db_sg,
             "--no-publicly-accessible", "--storage-encrypted", "--no-multi-az", "--backup-retention-period", "1",
             "--tags", json.dumps(tags)], region)

print("Waiting for RDS to become available; this can take several minutes.")
deadline = time.time() + 1200
while time.time() < deadline:
    item = lab.run_aws(["rds", "describe-db-instances", "--db-instance-identifier", db_identifier], region)["DBInstances"][0]
    status = item["DBInstanceStatus"]
    if status == "available":
        endpoint = item["Endpoint"]["Address"]
        db_env.write_text(f"DB_HOST={endpoint}\nDB_PORT=5432\nDB_NAME=inspection\nDB_USER=inspection\nDB_PASSWORD={password}\n", encoding="utf-8")
        os.chmod(db_env, 0o600)
        record["db_endpoint"] = endpoint
        record["db_status"] = status
        record["db_publicly_accessible"] = item["PubliclyAccessible"]
        save()
        print(json.dumps({"status": status, "publicly_accessible": item["PubliclyAccessible"],
                          "db_instance_id_suffix": db_identifier[-4:]}, indent=2))
        if item["PubliclyAccessible"] is not False:
            raise SystemExit("RDS is unexpectedly public; stop and inspect before continuing.")
        break
    print(f"RDS status: {status}")
    time.sleep(15)
else:
    raise SystemExit("Timed out waiting for RDS; inspect the recorded identifier before retrying.")
PY