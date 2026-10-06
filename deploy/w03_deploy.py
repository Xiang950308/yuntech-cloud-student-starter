#!/usr/bin/env python3
"""Reviewed W3 EC2 deployment and scoped cleanup helper."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "resources.json"
COURSE = "yuntech-115-1"
DEFAULT_GROUP = "4"
DEFAULT_OWNER = "Xiang"

sys.path.insert(0, str(ROOT / "scripts"))
import lab  # noqa: E402


def fail(message):
    raise lab.LabError(message)


def tags(group, owner):
    return {"course": COURSE, "week": "w03", "group": group, "owner": owner}


def tag_args(values):
    return [{"Key": key, "Value": value} for key, value in values.items()]


def tag_spec(values):
    return [{"ResourceType": resource, "Tags": [{"Key": key, "Value": value} for key, value in values.items()]}
            for resource in ("instance", "volume", "network-interface")]


def region_from(args):
    return args.region or lab.context()["region"]


def save(data):
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    RESOURCES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(RESOURCES, 0o600)


def load():
    if not RESOURCES.exists():
        fail(f"Missing {RESOURCES}; no untracked resource IDs will be searched for.")
    try:
        data = json.loads(RESOURCES.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot read {RESOURCES}: {type(exc).__name__}")
    for key in ("group", "owner", "tags", "instance_id", "security_group_id", "key_pair_name"):
        if key not in data:
            fail(f"Resource record missing {key}")
    return data


def describe_default_network(region):
    vpcs = lab.run_aws(["ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true"], region)["Vpcs"]
    if len(vpcs) != 1:
        fail(f"Expected exactly one default VPC, found {len(vpcs)}")
    vpc_id = vpcs[0]["VpcId"]
    subnets = lab.run_aws(["ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}",
                           "Name=default-for-az,Values=true"], region)["Subnets"]
    route_tables = lab.run_aws(["ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc_id}"], region)["RouteTables"]
    subnet_by_id = {item["SubnetId"]: item for item in subnets}
    public = []
    for table in route_tables:
        public_route = any(route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("GatewayId", "").startswith("igw-")
                           for route in table.get("Routes", []))
        if not public_route:
            continue
        associated = {item.get("SubnetId") for item in table.get("Associations", []) if item.get("SubnetId")}
        if associated:
            public.extend(subnet_by_id[subnet_id] for subnet_id in associated if subnet_id in subnet_by_id)
        elif any(item.get("Main") for item in table.get("Associations", [])):
            public.extend(subnets)
    unique = {item["SubnetId"]: item for item in public}
    if not unique:
        fail("No default subnet with an effective 0.0.0.0/0 route to an IGW")
    subnet = sorted(unique.values(), key=lambda item: item["SubnetId"])[0]
    return vpc_id, subnet


def find_ami(region):
    result = lab.run_aws(["ec2", "describe-images", "--owners", "amazon", "--filters",
                          "Name=state,Values=available", "Name=architecture,Values=x86_64",
                          "Name=root-device-type,Values=ebs", "Name=virtualization-type,Values=hvm",
                          "Name=name,Values=al2023-ami-2023.*-x86_64"], region)
    images = result.get("Images", [])
    if not images:
        fail("No Amazon Linux 2023 x86_64 AMI found")
    return max(images, key=lambda item: item.get("CreationDate", ""))


def source_cidr(value):
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}/32", value):
        fail("--source-cidr must be one IPv4 address with /32")
    if any(int(octet) > 255 for octet in value[:-3].split(".")):
        fail("--source-cidr contains an invalid IPv4 address")
    return value


def make_user_data(commit):
    output = LOCAL / "w03-user-data.sh"
    if output.exists():
        output.unlink()
    subprocess.run(["bash", str(ROOT / "deploy/make-user-data.sh"), commit, str(output)], cwd=ROOT, check=True)
    return output.read_text(encoding="utf-8")


def public_key(path):
    private = Path(path).expanduser()
    public = Path(str(private) + ".pub")
    if not private.exists():
        private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(private), "-C", "w03-group4-xiang"],
                       check=True, stdout=subprocess.DEVNULL)
        os.chmod(private, 0o600)
    if not public.exists():
        fail(f"Missing public key {public}; private key content was not read")
    return public


def wait_instance(region, instance_id, state):
    deadline = time.time() + 300
    while time.time() < deadline:
        item = lab.run_aws(["ec2", "describe-instances", "--instance-ids", instance_id], region)["Reservations"][0]["Instances"][0]
        if item["State"]["Name"] == state:
            return item
        time.sleep(5)
    fail(f"Timed out waiting for instance {instance_id} to become {state}")


def wait_status_checks(region, instance_id):
    deadline = time.time() + 300
    while time.time() < deadline:
        item = lab.run_aws(["ec2", "describe-instance-status", "--instance-ids", instance_id,
                            "--include-all-instances"], region).get("InstanceStatuses", [])
        if item and item[0]["InstanceState"]["Name"] == "running" and item[0]["SystemStatus"]["Status"] == "ok" \
                and item[0]["InstanceStatus"]["Status"] == "ok":
            return
        time.sleep(5)
    fail(f"Timed out waiting for EC2 status checks on {instance_id}")


def health(address, commit):
    url = f"http://{address}/health"
    deadline = time.time() + 300
    last = "no response"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                body = json.loads(response.read())
                if response.status == 200 and body.get("version") == commit and body.get("status") == "ok":
                    print(f"Health OK: HTTP 200, version {commit}")
                    return body
                last = f"HTTP {response.status} or unexpected version"
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(5)
    fail(f"Health check failed for {url}: {last}")


def up(args):
    region = region_from(args)
    ctx = lab.verify()
    if ctx["region"] != region:
        fail("--region does not match the verified Learner Lab context")
    values = tags(args.group, args.owner)
    commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}"], cwd=ROOT, text=True).strip()
    cidr = source_cidr(args.source_cidr)
    vpc_id, subnet = describe_default_network(region)
    ami = find_ami(region)
    key_path = Path(args.key_path).expanduser()
    key_name = f"w03-{args.group}-{args.owner.lower()}-{ctx['account'][-4:]}"
    print(json.dumps({"will_create": ["1 security group", "1 imported ed25519 key pair", "1 t3.micro EC2 instance"],
                      "network": {"vpc": vpc_id, "subnet": subnet["SubnetId"], "az": subnet["AvailabilityZone"], "source_32": cidr},
                      "ami": {"id": ami["ImageId"], "name": ami.get("Name"), "created": ami.get("CreationDate")},
                      "tags": values, "commit": commit, "costs": "t3.micro runtime, encrypted gp3 root EBS, public IPv4 while running",
                      "cleanup": "deploy/down.sh terminates by recorded ID, verifies ENI/EBS, then deletes SG and key pair"}, indent=2))
    lab.approve("Review the exact creation list above.", "CREATE-W03")
    user_data = make_user_data(args.commit)
    key_material = public_key(key_path)
    sg = lab.run_aws(["ec2", "create-security-group", "--group-name", key_name, "--description", "W3 inspection scoped SG",
                      "--vpc-id", vpc_id], region)
    sg_id = sg["GroupId"]
    record = {"region": region, "group": args.group, "owner": args.owner, "tags": values, "vpc_id": vpc_id,
              "subnet_id": subnet["SubnetId"], "security_group_id": sg_id, "key_pair_name": key_name,
              "key_path": str(key_path), "commit": commit}
    save(record)
    lab.run_aws(["ec2", "create-tags", "--resources", sg_id, "--tags", json.dumps(tag_args(values))], region)
    lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg_id, "--ip-permissions",
                 json.dumps([{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                               "IpRanges": [{"CidrIp": cidr, "Description": "Codespace /32"}]} for port in (22, 80)])], region)
    key_result = lab.run_aws(["ec2", "import-key-pair", "--key-name", key_name,
                              "--public-key-material", f"fileb://{key_material}"], region)
    record["key_pair_id"] = key_result.get("KeyPairId")
    save(record)
    record["instance_id"] = lab.run_aws(["ec2", "run-instances", "--image-id", ami["ImageId"], "--instance-type", "t3.micro",
                                          "--subnet-id", subnet["SubnetId"], "--security-group-ids", sg_id, "--key-name", key_name,
                                          "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
                                          "--block-device-mappings", json.dumps([{"DeviceName": ami.get("RootDeviceName", "/dev/xvda"),
                                                                                  "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True,
                                                                                          "DeleteOnTermination": True}}]),
                                          "--user-data", user_data, "--tag-specifications", json.dumps([
                                              {"ResourceType": "instance", "Tags": tag_args(values)},
                                              {"ResourceType": "volume", "Tags": tag_args(values)},
                                              {"ResourceType": "network-interface", "Tags": tag_args(values)}
                                          ]), "--count", "1"], region)["Instances"][0]["InstanceId"]
    save(record)
    instance = wait_instance(region, record["instance_id"], "running")
    record["public_ip"] = instance.get("PublicIpAddress")
    record["eni_id"] = instance["NetworkInterfaces"][0]["NetworkInterfaceId"]
    record["volume_id"] = instance["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
    save(record)
    wait_status_checks(region, record["instance_id"])
    if not record["public_ip"]:
        fail("Running instance has no public IPv4 address")
    health(record["public_ip"], commit)
    print(f"Created and verified {record['instance_id']}; IDs saved in {RESOURCES}")


def verify_owned(record, region):
    result = lab.run_aws(["ec2", "describe-instances", "--instance-ids", record["instance_id"]], region)
    instances = result.get("Reservations", [])
    if not instances:
        return None
    instance = instances[0]["Instances"][0]
    actual = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if any(actual.get(key) != value for key, value in record["tags"].items()):
        fail("Recorded instance tags do not match course/group/owner ownership")
    return instance


def down(args):
    record = load()
    region = args.region or record["region"]
    ctx = lab.verify()
    if ctx["region"] != region:
        fail("Region does not match the verified Learner Lab context")
    instance = verify_owned(record, region)
    if not instance:
        fail("Recorded instance does not exist; refusing to search by name")
    action = "stop the recorded instance" if args.stop else "terminate the recorded instance, verify its EBS and ENI disappear, delete the recorded SG and key pair"
    print(json.dumps({"will_do": action, "instance_id": record["instance_id"], "security_group_id": record["security_group_id"],
                      "key_pair_name": record["key_pair_name"], "tags": record["tags"]}, indent=2))
    lab.approve("Review the exact scoped cleanup above.", "STOP-W03" if args.stop else "DELETE-W03")
    if args.stop:
        lab.run_aws(["ec2", "stop-instances", "--instance-ids", record["instance_id"]], region)
        wait_instance(region, record["instance_id"], "stopped")
        record["state"] = "stopped"
        save(record)
        print("Recorded instance is stopped and retained.")
        return
    if instance["State"]["Name"] != "terminated":
        lab.run_aws(["ec2", "terminate-instances", "--instance-ids", record["instance_id"]], region)
        wait_instance(region, record["instance_id"], "terminated")
    for command, identifier, label in ((["ec2", "describe-volumes", "--volume-ids", record["volume_id"]], record["volume_id"], "EBS"),
                                       (["ec2", "describe-network-interfaces", "--network-interface-ids", record["eni_id"]], record["eni_id"], "ENI")):
        try:
            data = lab.run_aws(command, region)
        except lab.LabError as exc:
            if "NotFound" not in str(exc):
                raise
            continue
        if data.get("Volumes", data.get("NetworkInterfaces", [])):
            fail(f"{label} {identifier} still exists after termination")
    lab.run_aws(["ec2", "delete-security-group", "--group-id", record["security_group_id"]], region)
    lab.run_aws(["ec2", "delete-key-pair", "--key-name", record["key_pair_name"]], region)
    print("Verified absent: instance, root EBS, ENI, security group, imported key pair.")
    RESOURCES.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    up_parser = sub.add_parser("up")
    up_parser.add_argument("--group", default=DEFAULT_GROUP)
    up_parser.add_argument("--owner", default=DEFAULT_OWNER)
    up_parser.add_argument("--source-cidr", required=True)
    up_parser.add_argument("--commit", default="HEAD")
    up_parser.add_argument("--region")
    up_parser.add_argument("--key-path", default="~/.ssh/w03-group4-xiang")
    up_parser.set_defaults(func=up)
    down_parser = sub.add_parser("down")
    down_parser.add_argument("--stop", action="store_true")
    down_parser.add_argument("--region")
    down_parser.set_defaults(func=down)
    args = parser.parse_args()
    try:
        args.func(args)
    except (lab.LabError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())