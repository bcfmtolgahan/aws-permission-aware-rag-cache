#!/usr/bin/env python3
"""Read-only absence checks for this run's exact resource IDs after destroy."""
from datetime import datetime, timezone
import json
import subprocess

from botocore.exceptions import ClientError
from cloud import LOCAL, ROOT, clean_environment, guarded_session


def main():
    session, _ = guarded_session()
    resources = json.loads((LOCAL / "resources.json").read_text())
    run = json.loads((ROOT / "infra/sandbox.auto.tfvars.json").read_text())["run_id"]
    assert resources["name"] == f"parc-{run}"
    name = resources["name"]
    checks = []

    def absent(label, operation, codes):
        try:
            operation()
            checks.append({"resource": label, "absent": False})
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            checks.append({"resource": label, "absent": code in codes, "response": code})

    for key in ["api_function", "test_function"]:
        absent(key, lambda key=key: session.client("lambda").get_function(FunctionName=resources[key]),
               {"ResourceNotFoundException"})
    absent("DynamoDB table", lambda: session.client("dynamodb").describe_table(TableName=resources["table"]),
           {"ResourceNotFoundException"})
    absent("S3 bucket", lambda: session.client("s3").head_bucket(Bucket=resources["bucket"]), {"404", "NoSuchBucket"})
    absent("HTTP API", lambda: session.client("apigatewayv2").get_api(ApiId=resources["api_id"]), {"NotFoundException"})
    absent("Cognito pool and users", lambda: session.client("cognito-idp").describe_user_pool(UserPoolId=resources["pool_id"]),
           {"ResourceNotFoundException"})
    ecache = session.client("elasticache")
    absent("Valkey replication group", lambda: ecache.describe_replication_groups(ReplicationGroupId=resources["cache_id"]),
           {"ReplicationGroupNotFoundFault"})
    absent("Valkey subnet group", lambda: ecache.describe_cache_subnet_groups(CacheSubnetGroupName=name),
           {"CacheSubnetGroupNotFoundFault"})
    absent("Valkey parameter group", lambda: ecache.describe_cache_parameter_groups(CacheParameterGroupName=name),
           {"CacheParameterGroupNotFound", "CacheParameterGroupNotFoundFault"})
    absent("IAM role and inline policy", lambda: session.client("iam").get_role(RoleName=resources["role_name"]),
           {"NoSuchEntity"})
    ec2 = session.client("ec2")
    absent("VPC", lambda: ec2.describe_vpcs(VpcIds=[resources["vpc_id"]]), {"InvalidVpcID.NotFound"})
    for index, group in enumerate(resources["security_group_ids"]):
        absent(f"security group {index+1}", lambda group=group: ec2.describe_security_groups(GroupIds=[group]),
               {"InvalidGroup.NotFound"})
    endpoints = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [resources["vpc_id"]]}])["VpcEndpoints"]
    checks.append({"resource": "VPC endpoints", "absent": all(e["State"] == "deleted" for e in endpoints)})
    for label, method, key in [("subnets", "describe_subnets", "Subnets"),
                               ("route tables", "describe_route_tables", "RouteTables"),
                               ("network interfaces", "describe_network_interfaces", "NetworkInterfaces")]:
        found = getattr(ec2, method)(Filters=[{"Name": "vpc-id", "Values": [resources["vpc_id"]]}])[key]
        checks.append({"resource": label, "absent": not found})
    logs = session.client("logs").describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{name}-")["logGroups"]
    checks.append({"resource": "CloudWatch log groups", "absent": not logs})
    snapshots = ecache.describe_snapshots(SnapshotSource="manual")["Snapshots"]
    checks.append({"resource": "manual Valkey snapshots", "absent": not any(s.get("ReplicationGroupId") == name for s in snapshots)})
    state = subprocess.run(["terraform", "state", "list"], cwd=ROOT / "infra", env=clean_environment(),
                           check=True, text=True, capture_output=True).stdout.strip()
    checks.append({"resource": "Terraform managed resources", "absent": not state})
    stamp = json.loads((LOCAL / "lifecycle.json").read_text())
    report = {"execution_scope": "dedicated sandbox", "region": "eu-central-1", **stamp,
              "verified_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
              "all_absent": all(c["absent"] for c in checks)}
    recovery = LOCAL / "cleanup-role.json"
    if recovery.exists() and json.loads(recovery.read_text()).get("vpc_id") == resources["vpc_id"]:
        report["execution_role_recovered_during_teardown"] = True
        report["temporary_recovery_role_removed"] = (LOCAL / "cleanup-role-removed.json").exists()
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/cleanup.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["all_absent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
