"""Deploy explicitly selected CTRL Loop environments using reviewed resource bindings.

Run with an AWS SSO session and boto3. This script never prints
secret values. Local state and bootstrap credentials live under ignored .local/.
No existing service, database, domain, or CloudFront distribution is replaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlparse
from uuid import uuid4
import zipfile

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values
from deployment_config import ROOT, load_environment, migrate_legacy_dev


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ensure_call(fn, expected, **kwargs):
    try:
        return fn(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in expected:
            raise
        return None


def after_iam_propagation(fn, **kwargs):
    """New execution roles may need a few seconds before another AWS service sees them."""
    for attempt in range(4):
        try:
            return fn(**kwargs)
        except ClientError as exc:
            message = exc.response["Error"].get("Message", "").lower()
            if attempt == 3 or not any(p in message for p in ("assume the role", "sts:assumerole")):
                raise
            time.sleep(5)


def fleet_credentials(path):
    source = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    values = {name: source.get(name) for name in ("CUA_CLIENT_ID", "CUA_CLIENT_SECRET")}
    if not all(values.values()):
        raise RuntimeError("Fleet credential file is missing required values")
    return values


def account_email_configuration(config):
    email = config.raw.get("email", {})
    provider = email.get("provider")
    if provider not in ("smtp", "ses") or not email.get("from"):
        raise RuntimeError("Configure real email.provider (smtp or ses) and email.from before deploying account signup")
    values = {"EMAIL_PROVIDER": provider, "EMAIL_FROM": email["from"], "AWS_REGION": config["region"]}
    secret_names = []
    if provider == "smtp":
        if not email.get("smtp_host") or email.get("smtp_security", "starttls") not in ("starttls", "ssl"):
            raise RuntimeError("Configure SMTP host and secure transport before deployment")
        values.update(SMTP_HOST=email["smtp_host"], SMTP_PORT=str(email.get("smtp_port", 465 if email.get("smtp_security") == "ssl" else 587)),
                      SMTP_SECURITY=email.get("smtp_security", "starttls"))
        if email.get("smtp_auth", True):
            secret_names = ["SMTP_USERNAME", "SMTP_PASSWORD"]
    return values, secret_names


class Deployment:
    def __init__(self, profile, environment):
        self.config = load_environment(environment)
        self.local = self.config.state_dir
        self.state_path = self.config.state_path
        self.secret_path = self.config.secret_path
        self.name = self.config.resource_name
        for key in ("account", "region", "vpc", "subnets", "alb_sg", "db_sg", "listener", "db_host", "db_name", "db_user", "cluster", "openai_secret"):
            setattr(self, key, self.config[key])
        self.tags = [{"Key": "Project", "Value": "ctrlloop"}, {"Key": "Environment", "Value": environment}]
        self.lower_tags = [{"key": item["Key"], "value": item["Value"]} for item in self.tags]
        self.session = boto3.Session(profile_name=profile or self.config["default_profile"], region_name=self.region)
        actual = self.session.client("sts").get_caller_identity()["Account"]
        if actual != self.account:
            raise RuntimeError("Refusing deployment to an unexpected AWS account")
        self.state = load(self.state_path)
        self.secret = load(self.secret_path)
        self.validate_state()

    def client(self, name):
        return self.session.client(name)

    def record(self, **kwargs):
        self.state.update(kwargs)
        self.state.update(environment=self.config.name, account=self.account, region=self.region)
        self.validate_state()
        save(self.state_path, self.state)

    def validate_state(self):
        """Reject a copied dev/prod state before it can select another environment."""
        exact = {"environment": self.config.name, "account": self.account, "region": self.region,
                 "service": self.name, "database": self.db_name, "database_host": self.db_host,
                 "repository": f"{self.account}.dkr.ecr.{self.region}.amazonaws.com/{self.name}",
                 "execution_role": f"arn:aws:iam::{self.account}:role/{self.name}-execution",
                 "task_role": f"arn:aws:iam::{self.account}:role/{self.name}-app",
                 "report_bucket": f"{self.name}-reports-{self.account}-{self.region}",
                 "build_source_bucket": f"{self.name}-build-{self.account}-{self.region}"}
        for key in ("distribution", "url"):
            expected = self.config.raw.get("expected_" + key)
            if expected:
                exact[key] = expected
        for key, expected in exact.items():
            if key in self.state and self.state[key] != expected:
                raise RuntimeError(f"State {key} does not belong to the selected {self.config.name} environment")
        if self.config.name == "prod":
            dev = load_environment("dev")
            if self.state.get("distribution") == dev["expected_distribution"] or self.state.get("url") == dev["expected_url"]:
                raise RuntimeError("Production cannot reuse the development distribution/origin")
        prefixes = {
            "app_secret_arn": f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{self.config['app_secret_name']}-",
            "target_group": f"arn:aws:elasticloadbalancing:{self.region}:{self.account}:targetgroup/{self.name}/",
            "task_definition": f"arn:aws:ecs:{self.region}:{self.account}:task-definition/{self.name}:",
            "build_id": f"{self.name}:",
            "listener_rule": self.listener.replace(":listener/", ":listener-rule/") + "/",
        }
        for key, prefix in prefixes.items():
            if key in self.state and not self.state[key].startswith(prefix):
                raise RuntimeError(f"State {key} does not match the configured resource")
        if self.secret.get("DATABASE_URL"):
            url = urlparse(self.secret["DATABASE_URL"])
            if (url.hostname, url.username, url.path) != (self.db_host, self.db_user, "/" + self.db_name):
                raise RuntimeError("Private database credentials belong to a different environment")

    def guard_resources(self):
        """Read-only validation of shared account/VPC/DB bindings before writes."""
        if not self.config["enabled"]:
            raise RuntimeError(f"{self.config.name} is disabled pending reviewed environment/domain configuration")
        ec2 = self.client("ec2")
        subnets = ec2.describe_subnets(SubnetIds=self.subnets)["Subnets"]
        if len(subnets) != len(self.subnets) or any(s["VpcId"] != self.vpc for s in subnets):
            raise RuntimeError("Configured subnets do not belong to the expected VPC")
        ids = [self.alb_sg, self.db_sg] + ([self.state["security_group"]] if self.state.get("security_group") else [])
        groups = ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"]
        if any(group["VpcId"] != self.vpc for group in groups):
            raise RuntimeError("Configured security groups do not belong to the expected VPC")
        if self.state.get("security_group") and not any(g["GroupId"] == self.state["security_group"] and g["GroupName"] == self.name for g in groups):
            raise RuntimeError("Application security group belongs to another service")
        elb = self.client("elbv2")
        listener = elb.describe_listeners(ListenerArns=[self.listener])["Listeners"][0]
        alb = elb.describe_load_balancers(LoadBalancerArns=[listener["LoadBalancerArn"]])["LoadBalancers"][0]
        if alb["VpcId"] != self.vpc or self.alb_sg not in alb.get("SecurityGroups", []):
            raise RuntimeError("Listener does not belong to the configured VPC/ALB security group")
        dbs = self.client("rds").describe_db_clusters()["DBClusters"]
        database = next((db for db in dbs if db["Endpoint"] == self.db_host), None)
        if not database or self.db_sg not in [group["VpcSecurityGroupId"] for group in database["VpcSecurityGroups"]]:
            raise RuntimeError("Database endpoint/security-group bindings are not verified")
        clusters = self.client("ecs").describe_clusters(clusters=[self.cluster])["clusters"]
        if len(clusters) != 1 or clusters[0]["status"] != "ACTIVE":
            raise RuntimeError("Expected ECS cluster is unavailable")
        if self.state.get("target_group"):
            target = elb.describe_target_groups(TargetGroupArns=[self.state["target_group"]])["TargetGroups"][0]
            if target["VpcId"] != self.vpc or target["TargetGroupName"] != self.name:
                raise RuntimeError("Target group does not match the selected environment")
        self.openai_secret = self.client("secretsmanager").describe_secret(SecretId=self.openai_secret)["ARN"]

    def prepare(self, fleet_env):
        sm = self.client("secretsmanager")
        if self.state.get("app_secret_arn"):
            # AWS is authoritative; a cached local copy can predate shared-key
            # imports or an administrator's unrelated secret update.
            self.secret = json.loads(sm.get_secret_value(SecretId=self.state["app_secret_arn"])["SecretString"])
            self.validate_state()
            save(self.secret_path, self.secret)
        if not self.secret:
            self.secret = {
                "DATABASE_PASSWORD": secrets.token_urlsafe(36),
                "ORIGIN_VERIFY": secrets.token_urlsafe(36),
                **fleet_credentials(fleet_env),
            }
            self.secret["DATABASE_URL"] = (
                f"postgresql://{self.db_user}:{quote(self.secret['DATABASE_PASSWORD'], safe='')}"
                f"@{self.db_host}:5432/{self.db_name}?sslmode=require"
            )
            save(self.secret_path, self.secret)
        if "app_secret_arn" not in self.state:
            try:
                result = sm.create_secret(Name=self.config["app_secret_name"], SecretString=json.dumps(self.secret), Tags=self.tags)
            except sm.exceptions.ResourceExistsException:
                # Recover existing credentials rather than rotating an active deployment.
                result = sm.get_secret_value(SecretId=self.config["app_secret_name"])
                self.secret = json.loads(result["SecretString"])
                self.validate_state()
                save(self.secret_path, self.secret)
            self.record(app_secret_arn=result["ARN"])
        self.network()
        self.execution_role()
        self.edge()
        if self.config.name == "dev":
            ecr = self.client("ecr")
            try:
                repo = ecr.describe_repositories(repositoryNames=[self.name])["repositories"][0]
            except ecr.exceptions.RepositoryNotFoundException:
                repo = ecr.create_repository(repositoryName=self.name, imageScanningConfiguration={"scanOnPush": True},
                                             imageTagMutability="IMMUTABLE", tags=self.tags)["repository"]
            if repo["imageTagMutability"] != "IMMUTABLE":
                raise RuntimeError("Development image repository must use immutable tags")
            self.record(repository=repo["repositoryUri"])
        logs = self.client("logs")
        ensure_call(logs.create_log_group, {"ResourceAlreadyExistsException"}, logGroupName=f"/ecs/{self.name}")
        logs.put_retention_policy(logGroupName=f"/ecs/{self.name}", retentionInDays=14)
        print(json.dumps({"prepared": True, "url": self.state["url"], "state": str(self.state_path)}))

    def database(self):
        """Start a one-off initializer inside the VPC, with no public DB ingress."""
        iam = self.client("iam")
        role_name = f"{self.name}-db-bootstrap"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=self.tags)
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
        admin_secret = self.client("secretsmanager").describe_secret(SecretId=self.config["db_admin_secret"])["ARN"]
        iam.put_role_policy(RoleName=role_name, PolicyName="bootstrap-secrets", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [self.state["app_secret_arn"], admin_secret]}]}))
        init_code = '''import os
import psycopg
from psycopg import sql
user = os.environ["DB_USER"]
database_name = os.environ["DB_NAME"]
admin = os.environ["DB_ADMIN_USER"]
with psycopg.connect(host=os.environ["DB_HOST"], dbname="postgres", user=admin, password=os.environ["DB_ADMIN_PASSWORD"], sslmode="require", connect_timeout=20, autocommit=True) as conn:
    if not conn.execute("SELECT rolname FROM pg_roles WHERE rolname=%s", (user,)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(user), sql.Literal(os.environ["DB_PASSWORD"])))
    database = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (database_name,)).fetchone()
    if not database:
        conn.execute(sql.SQL("GRANT {} TO {} WITH SET TRUE").format(sql.Identifier(user), sql.Identifier(admin)))
        try:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database_name), sql.Identifier(user)))
        finally:
            conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(user), sql.Identifier(admin)))
    elif database[0] != user:
        raise RuntimeError("Existing CTRL Loop database has an unexpected owner")
with psycopg.connect(host=os.environ["DB_HOST"], dbname=database_name, user=user, password=os.environ["DB_PASSWORD"], sslmode="require", connect_timeout=20) as conn:
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database_name)))
    conn.execute("SELECT 1")
print("CTRL Loop dedicated database created and login verified")
'''
        ecs = self.client("ecs")
        task = ecs.register_task_definition(family=f"{self.name}-db-init", networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="512",
            executionRoleArn=iam.get_role(RoleName=role_name)["Role"]["Arn"],
            containerDefinitions=[{"name": "init", "image": "public.ecr.aws/docker/library/python:3.12-slim", "essential": True,
                "entryPoint": ["sh", "-c"], "command": ["pip install --quiet 'psycopg[binary]==3.3.6' && python -c \"$INIT_CODE\""],
                "environment": [{"name": "INIT_CODE", "value": init_code}, {"name": "DB_USER", "value": self.db_user}, {"name": "DB_NAME", "value": self.db_name}, {"name": "DB_HOST", "value": self.db_host}, {"name": "DB_ADMIN_USER", "value": self.config["db_admin_user"]}],
                "secrets": [{"name": "DB_ADMIN_PASSWORD", "valueFrom": admin_secret}, {"name": "DB_PASSWORD", "valueFrom": f"{self.state['app_secret_arn']}:DATABASE_PASSWORD::"}],
                "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": f"/ecs/{self.name}", "awslogs-region": self.region, "awslogs-stream-prefix": "bootstrap"}}}], tags=self.lower_tags)["taskDefinition"]
        result = after_iam_propagation(ecs.run_task, cluster=self.cluster, taskDefinition=task["taskDefinitionArn"], launchType="FARGATE", platformVersion="1.4.0",
            networkConfiguration={"awsvpcConfiguration": {"subnets": self.subnets, "securityGroups": [self.state["security_group"]], "assignPublicIp": "DISABLED"}}, tags=self.lower_tags)
        if result.get("failures"):
            raise RuntimeError(f"DB init task launch failed: {result['failures']}")
        self.record(db_init_task=result["tasks"][0]["taskArn"], database=self.db_name, database_host=self.db_host, database_ready=False)
        print(json.dumps({"database_init_started": self.state["db_init_task"]}))

    def network(self):
        ec2 = self.client("ec2")
        groups = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [self.vpc]},
                                                       {"Name": "group-name", "Values": [self.name]}])["SecurityGroups"]
        group_id = groups[0]["GroupId"] if groups else ec2.create_security_group(
            GroupName=self.name, Description="CTRL Loop MVP service; ingress only from existing ALB", VpcId=self.vpc,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tags}])["GroupId"]
        self.record(security_group=group_id)
        ensure_call(ec2.authorize_security_group_ingress, {"InvalidPermission.Duplicate"}, GroupId=group_id,
                    IpPermissions=[{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
                                    "UserIdGroupPairs": [{"GroupId": self.alb_sg, "Description": "CTRL Loop from shared ALB"}]}])
        ensure_call(ec2.authorize_security_group_ingress, {"InvalidPermission.Duplicate"}, GroupId=self.db_sg,
                    IpPermissions=[{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                                    "UserIdGroupPairs": [{"GroupId": group_id, "Description": "CTRL Loop dedicated database role"}]}])
        elb = self.client("elbv2")
        try:
            group = elb.describe_target_groups(Names=[self.name])["TargetGroups"][0]
        except elb.exceptions.TargetGroupNotFoundException:
            group = elb.create_target_group(Name=self.name, Protocol="HTTP", Port=8080, VpcId=self.vpc, TargetType="ip",
                HealthCheckPath="/healthz", HealthCheckProtocol="HTTP", HealthCheckIntervalSeconds=15,
                HealthCheckTimeoutSeconds=5, HealthyThresholdCount=2, UnhealthyThresholdCount=3,
                Matcher={"HttpCode": "200"}, Tags=self.tags)["TargetGroups"][0]
        self.record(target_group=group["TargetGroupArn"])
        elb.modify_target_group_attributes(TargetGroupArn=group["TargetGroupArn"],
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}])
        if "listener_rule" not in self.state:
            rules = elb.describe_rules(ListenerArn=self.listener)["Rules"]
            existing = next((r for r in rules if any(a.get("TargetGroupArn") == group["TargetGroupArn"] for a in r["Actions"])), None)
            if existing:
                rule = existing
            else:
                priorities = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
                priority = next(p for p in range(110, 200) if p not in priorities)
                rule = elb.create_rule(ListenerArn=self.listener, Priority=priority,
                    Conditions=[{"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "x-ctrlloop-origin",
                        "Values": [self.secret["ORIGIN_VERIFY"]]}}],
                    Actions=[{"Type": "forward", "TargetGroupArn": group["TargetGroupArn"]}], Tags=self.tags)["Rules"][0]
            self.record(listener_rule=rule["RuleArn"])

    def execution_role(self):
        iam = self.client("iam")
        role_name = f"{self.name}-execution"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=self.tags)
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
        iam.put_role_policy(RoleName=role_name, PolicyName="ctrl-loop-secrets", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [self.state["app_secret_arn"], self.openai_secret]}]}))
        self.record(execution_role=iam.get_role(RoleName=role_name)["Role"]["Arn"])

    def edge(self):
        cf = self.client("cloudfront")
        if "distribution" not in self.state:
            if "origin_policy" not in self.state:
                origin_policy = cf.create_origin_request_policy(OriginRequestPolicyConfig={
                    "Name": f"{self.name}-headers-{int(time.time())}", "Comment": "Session cookies and WebSocket headers; origin host retained",
                    "HeadersConfig": {"HeaderBehavior": "allExcept", "Headers": {"Quantity": 1, "Items": ["host"]}},
                    "CookiesConfig": {"CookieBehavior": "all"}, "QueryStringsConfig": {"QueryStringBehavior": "all"}})
                self.record(origin_policy=origin_policy["OriginRequestPolicy"]["Id"])
            dist = cf.create_distribution_with_tags(DistributionConfigWithTags={"Tags": {"Items": self.tags}, "DistributionConfig": {
                "CallerReference": f"{self.name}-{int(time.time())}", "Comment": "CTRL Loop MVP authenticated application and native desktop viewer",
                "Enabled": True, "PriceClass": "PriceClass_100", "HttpVersion": "http2", "IsIPV6Enabled": True,
                "Origins": {"Quantity": 1, "Items": [{"Id": self.name, "DomainName": self.config["origin_host"],
                    "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "x-ctrlloop-origin", "HeaderValue": self.secret["ORIGIN_VERIFY"]}]},
                    "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}, "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}}]},
                "DefaultCacheBehavior": {"TargetOriginId": self.name, "ViewerProtocolPolicy": "redirect-to-https",
                    "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
                        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
                    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad", "OriginRequestPolicyId": self.state["origin_policy"],
                    "Compress": True, "TrustedSigners": {"Enabled": False, "Quantity": 0},
                    "TrustedKeyGroups": {"Enabled": False, "Quantity": 0}},
                "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
                "CustomErrorResponses": {"Quantity": 2, "Items": [{"ErrorCode": 502, "ErrorCachingMinTTL": 0}, {"ErrorCode": 503, "ErrorCachingMinTTL": 0}]},
            }})["Distribution"]
            self.record(distribution=dist["Id"], url=f"https://{dist['DomainName']}")
        print("CloudFront HTTPS distribution configured with caching disabled and WebSocket headers")

    def report_storage(self):
        """Private durable run evidence, separate from expiring build staging."""
        bucket = f"{self.name}-reports-{self.account}-{self.region}"
        s3 = self.client("s3")
        ensure_call(s3.create_bucket, {"BucketAlreadyOwnedByYou"}, Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": self.region})
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={k: True for k in [
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"]})
        s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
        s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": self.tags})
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
        iam = self.client("iam")
        role_name = f"{self.name}-app"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(trust), Tags=self.tags)
        iam.put_role_policy(RoleName=role_name, PolicyName="run-evidence", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{bucket}/companies/*"},
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/runs/*"},
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"}]}))
        email = self.config.raw.get("email", {})
        if email.get("provider") == "ses" and email.get("from"):
            iam.put_role_policy(RoleName=role_name, PolicyName="account-email", PolicyDocument=json.dumps({
                "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ses:SendEmail",
                "Resource": "*", "Condition": {"StringEquals": {"ses:FromAddress": email["from"]}}}]}))
        self.record(report_bucket=bucket, task_role=iam.get_role(RoleName=role_name)["Role"]["Arn"])
        print(json.dumps({"report_bucket": bucket, "public_access": "blocked", "retention": "durable; no automatic expiry"}))

    def build(self, revision, release_only=False):
        """Use managed ephemeral CodeBuild so local Docker/WSL is not required."""
        if self.config.name != "dev":
            raise RuntimeError("Build in dev, review its immutable image, then explicitly promote that image to prod")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", revision):
            raise ValueError("Revision must be a safe immutable image tag")
        self.local.mkdir(parents=True, exist_ok=True)
        s3 = self.client("s3")
        bucket = f"{self.name}-build-{self.account}-{self.region}"
        if not release_only:
            ensure_call(s3.create_bucket, {"BucketAlreadyOwnedByYou"}, Bucket=bucket,
                        CreateBucketConfiguration={"LocationConstraint": self.region})
            s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={k: True for k in [
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"]})
            s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
            s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
                {"ID": "expire-build-sources", "Status": "Enabled", "Filter": {"Prefix": "source/"}, "Expiration": {"Days": 7}}]})
            logs = self.client("logs")
            ensure_call(logs.create_log_group, {"ResourceAlreadyExistsException"}, logGroupName=f"/aws/codebuild/{self.name}")
            logs.put_retention_policy(logGroupName=f"/aws/codebuild/{self.name}", retentionInDays=14)
        archive = self.local / f"source-{revision}-{uuid4().hex}.zip"
        allow = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "README.md", "src", "web", "vendor"]
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
            for item in allow:
                path = ROOT / item
                if not path.exists():
                    continue
                paths = path.rglob("*") if path.is_dir() else [path]
                for file in paths:
                    if file.is_file() and not any(p in {".local", ".git", ".venv", "__pycache__", "node_modules"} for p in file.parts) and not file.name.startswith(".env") and file.suffix not in {".pyc", ".pem", ".key"}:
                        out.write(file, file.relative_to(ROOT).as_posix())
        # Freeze the exact bytes and bind this build's source key to their hash.
        # A reused revision cannot overwrite source another build is consuming.
        source_bytes = archive.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        key = f"source/{revision}/{source_sha256}.zip"
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=source_bytes, IfNoneMatch="*")
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise RuntimeError("Source already exists or another upload is active; use a new unique revision") from None
            raise
        role_name = f"{self.name}-build"
        if not release_only:
            iam = self.client("iam")
            trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
            ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=self.tags)
            iam.put_role_policy(RoleName=role_name, PolicyName="build-image", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/source/*"},
                {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": f"arn:aws:s3:::{bucket}"},
                {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": f"arn:aws:ecr:{self.region}:{self.account}:repository/{self.name}"},
                {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/codebuild/{self.name}*"},
            ]}))
        repo = self.state["repository"]
        buildspec = f"""version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {self.account}.dkr.ecr.{self.region}.amazonaws.com
  build:
    commands:
      - docker build --platform linux/amd64 -t {repo}:{revision} .
  post_build:
    commands:
      - docker push {repo}:{revision}
"""
        config = {"name": self.name, "source": {"type": "S3", "location": f"{bucket}/{key}", "buildspec": buildspec},
            "artifacts": {"type": "NO_ARTIFACTS"}, "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True}, "serviceRole": f"arn:aws:iam::{self.account}:role/{role_name}",
            "timeoutInMinutes": 20, "queuedTimeoutInMinutes": 10}
        cb = self.client("codebuild")
        projects = cb.batch_get_projects(names=[self.name])["projects"]
        if release_only:
            if len(projects) != 1 or projects[0]["serviceRole"] != config["serviceRole"]:
                raise RuntimeError("Release requires the existing development build project and exact build role")
        elif projects:
            cb.update_project(**config)
        else:
            after_iam_propagation(cb.create_project, **config, tags=self.lower_tags)
        # Per-build source/spec overrides avoid mutating another developer's
        # project settings. The existing build role limits output to dev ECR.
        result = cb.start_build(projectName=self.name, sourceTypeOverride="S3",
                                sourceLocationOverride=f"{bucket}/{key}", buildspecOverride=buildspec)
        self.record(build_id=result["build"]["id"], revision=revision, image=f"{repo}:{revision}", build_source_bucket=bucket,
                    build_source_key=key, source_sha256=source_sha256)
        print(json.dumps({"build_started": self.state["build_id"], "revision": revision}))

    def reviewed_manifest(self, path):
        manifest = load(path)
        dev = load_environment("dev")
        expected = {"schema_version": 1, "source_environment": "dev", "target_environment": "prod",
                    "account": self.account, "region": self.region,
                    "repository": f"{dev['account']}.dkr.ecr.{dev['region']}.amazonaws.com/{dev.resource_name}"}
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Promotion manifest identity does not match dev-to-prod in this account")
        for key in ("reviewed_by", "review_reference", "reviewed_at", "build_id", "source_sha256", "dev_task_definition"):
            if not isinstance(manifest.get(key), str) or not manifest[key].strip():
                raise RuntimeError(f"Promotion manifest is missing {key}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", manifest.get("revision", "")):
            raise RuntimeError("Invalid reviewed revision")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("image_digest", "")) or not re.fullmatch(r"[0-9a-f]{64}", manifest["source_sha256"]):
            raise RuntimeError("Promotion requires exact image and source digests")
        if not manifest["build_id"].startswith(dev.resource_name + ":"):
            raise RuntimeError("Promotion build does not belong to development")
        builds = self.client("codebuild").batch_get_builds(ids=[manifest["build_id"]])["builds"]
        if len(builds) != 1 or builds[0]["buildStatus"] != "SUCCEEDED" or builds[0]["projectName"] != dev.resource_name:
            raise RuntimeError("Reviewed development build is not verified successful")
        expected_source = f"{dev.resource_name}-build-{self.account}-{self.region}/source/{manifest['revision']}/{manifest['source_sha256']}.zip"
        if builds[0].get("source", {}).get("location") != expected_source:
            raise RuntimeError("Reviewed build source/revision does not match")
        image = self.client("ecr").describe_images(repositoryName=dev.resource_name,
            imageIds=[{"imageTag": manifest["revision"]}])["imageDetails"][0]
        if image["imageDigest"] != manifest["image_digest"]:
            raise RuntimeError("Reviewed immutable image digest no longer matches the registry")
        task = self.client("ecs").describe_task_definition(taskDefinition=manifest["dev_task_definition"])["taskDefinition"]
        if task["family"] != dev.resource_name or not any(c.get("image") == f"{manifest['repository']}@{manifest['image_digest']}" for c in task["containerDefinitions"]):
            raise RuntimeError("Reviewed image was not the referenced development task image")
        return manifest

    def promotion_manifest(self, revision, reviewed_by, review_reference):
        if self.config.name != "dev" or revision != self.state.get("revision"):
            raise RuntimeError("Review must name the current explicit development revision")
        if not reviewed_by.strip() or not review_reference.strip():
            raise ValueError("An identified reviewer and concrete review reference are required")
        ecs = self.client("ecs")
        service = ecs.describe_services(cluster=self.cluster, services=[self.name])["services"][0]
        if service["runningCount"] != 1 or service["pendingCount"] or len(service["deployments"]) != 1 or service["deployments"][0].get("rolloutState") != "COMPLETED":
            raise RuntimeError("Development must have one stable deployment before review")
        if service["taskDefinition"] != self.state.get("task_definition"):
            raise RuntimeError("Development service is not running the locally recorded revision")
        targets = self.client("elbv2").describe_target_health(TargetGroupArn=self.state["target_group"])["TargetHealthDescriptions"]
        if not targets or any(t["TargetHealth"]["State"] != "healthy" for t in targets):
            raise RuntimeError("Development targets are not all healthy")
        manifest = {"schema_version": 1, "source_environment": "dev", "target_environment": "prod",
            "account": self.account, "region": self.region, "repository": self.state["repository"],
            "revision": revision, "image_digest": self.state["image_digest"], "source_sha256": self.state["source_sha256"],
            "build_id": self.state["build_id"], "dev_task_definition": service["taskDefinition"],
            "reviewed_by": reviewed_by.strip(), "review_reference": review_reference.strip(),
            "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        path = self.local / "promotions" / f"{revision}.json"
        if path.exists():
            raise RuntimeError("Review manifest already exists; preserve the existing review record")
        save(path, manifest)
        try:
            self.reviewed_manifest(path)
        except Exception:
            # A failed review cannot be mistaken for an approved promotion file.
            path.rename(path.with_suffix(".invalid.json"))
            raise
        print(json.dumps({"review_manifest": str(path), "revision": revision, "deployed_to_prod": False}))

    def deploy(self, promotion_manifest=None, release_only=False, company_cutover=False):
        email_environment, email_secrets = account_email_configuration(self.config)
        if not self.state.get("database_ready"):
            raise RuntimeError("Database initialization is not verified. Run database-ready after it finishes.")
        if self.config.name == "prod":
            if not promotion_manifest:
                raise RuntimeError("Production deployment requires an explicit reviewed promotion manifest")
            approved = self.reviewed_manifest(promotion_manifest)
            image_uri = f"{approved['repository']}@{approved['image_digest']}"
            self.record(revision=approved["revision"], image_digest=approved["image_digest"], promotion=approved)
        else:
            if promotion_manifest:
                raise RuntimeError("A promotion manifest is only accepted for explicit production deployment")
            build = self.client("codebuild").batch_get_builds(ids=[self.state["build_id"]])["builds"][0]
            if build["buildStatus"] != "SUCCEEDED" or build["projectName"] != self.name:
                raise RuntimeError("Refusing deployment until the current environment image build succeeds")
            expected_key = f"source/{self.state['revision']}/{self.state['source_sha256']}.zip"
            if self.state.get("build_source_key") != expected_key or build.get("source", {}).get("location") != f"{self.state['build_source_bucket']}/{expected_key}":
                raise RuntimeError("Build source does not match the recorded revision/hash; rebuild with the current release helper")
            image = self.client("ecr").describe_images(repositoryName=self.name, imageIds=[{"imageTag": self.state["revision"]}])["imageDetails"][0]
            self.record(image_digest=image["imageDigest"])
            image_uri = f"{self.state['repository']}@{self.state['image_digest']}"
        ecs = self.client("ecs")
        services = ecs.describe_services(cluster=self.cluster, services=[self.name])["services"]
        current = services[0] if services and services[0]["status"] == "ACTIVE" else None
        legacy = False
        if current:
            current_task = ecs.describe_task_definition(taskDefinition=current["taskDefinition"])["taskDefinition"]
            current_app = next(c for c in current_task["containerDefinitions"] if c["name"] == "app")
            current_env = {item["name"]: item["value"] for item in current_app.get("environment", [])}
            legacy = current_env.get("COMPANY_SCHEMA_VERSION") != "1"
            if current_env.get("APP_ENV") not in (None, self.config.name):
                raise RuntimeError("Existing service belongs to a different application environment")
        if legacy and not company_cutover:
            raise RuntimeError("Earlier account schema requires --company-cutover to terminate old tasks before starting company accounts")
        if legacy and release_only:
            raise RuntimeError("First company cutover requires the full administrative deployment to update report and email permissions")
        if company_cutover and not legacy:
            raise RuntimeError("Company cutover is only valid when replacing an earlier account schema")
        stable_current = bool(current and len(current["deployments"]) == 1
                              and current["deployments"][0].get("rolloutState") == "COMPLETED"
                              and current["deployments"][0]["taskDefinition"] == current["taskDefinition"])
        if company_cutover and not stable_current:
            raise RuntimeError("Inspect and stabilize the existing service before the company cutover")
        # After a failed first cutover, ECS's last successful deployment can
        # still be the legacy app. Only a proven stable company-aware current
        # deployment is a safe automatic rollback target.
        allow_rollback = stable_current and not legacy
        if release_only:
            if len(services) != 1 or services[0]["status"] != "ACTIVE":
                raise RuntimeError("Release-only deployment requires the existing service")
            if len(services[0]["deployments"]) != 1 or services[0]["deployments"][0].get("rolloutState") != "COMPLETED":
                raise RuntimeError("Another release is active; coordinate and wait before deploying")
        if not release_only:
            self.report_storage()
        elif not self.state.get("task_role") or not self.state.get("report_bucket"):
            raise RuntimeError("An administrator must provision report storage before a release-only deployment")
        env = {"OPENAI_MODEL": "gpt-6-luna", "APP_ORIGIN": self.state["url"], "APP_ENV": self.config.name,
               "DEPLOY_REVISION": self.state["revision"],
               "REPORT_BUCKET": self.state["report_bucket"], "AWS_DEFAULT_REGION": self.region}
        secret_names = ["DATABASE_URL", "CUA_CLIENT_ID", "CUA_CLIENT_SECRET"]
        env.update(email_environment, COMPANY_SCHEMA_VERSION="1")
        secret_names += email_secrets
        task = ecs.register_task_definition(family=self.name, networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="1024",
            executionRoleArn=self.state["execution_role"], taskRoleArn=self.state["task_role"],
            runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            containerDefinitions=[{"name": "app", "image": image_uri, "essential": True,
                "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                "environment": [{"name": k, "value": v} for k, v in env.items()],
                "secrets": [{"name": k, "valueFrom": f"{self.state['app_secret_arn']}:{k}::"} for k in secret_names] + [{"name": "OPENAI_API_KEY", "valueFrom": self.openai_secret}],
                "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": f"/ecs/{self.name}", "awslogs-region": self.region, "awslogs-stream-prefix": "app"}}}],
            tags=self.lower_tags)["taskDefinition"]
        self.record(task_definition=task["taskDefinitionArn"])
        if company_cutover:
            # Old readers follow the previous account and session contracts.
            # They must never overlap the new company model. Draining only ALB targets does not terminate WebSockets.
            # Capture even STOPPING tasks, then require complete process exit.
            old_tasks = set()
            for desired_status in ("RUNNING", "STOPPED"):
                for page in ecs.get_paginator("list_tasks").paginate(cluster=self.cluster,
                        serviceName=self.name, desiredStatus=desired_status):
                    old_tasks.update(page["taskArns"])
            ecs.update_service(cluster=self.cluster, service=self.name, desiredCount=0,
                deploymentConfiguration={"deploymentCircuitBreaker": {"enable": True, "rollback": False}})
            print(json.dumps({"company_cutover": "stopping_legacy_tasks", "captured_tasks": len(old_tasks)}), flush=True)
            for attempt in range(90):
                remaining = []
                for desired_status in ("RUNNING", "STOPPED"):
                    for page in ecs.get_paginator("list_tasks").paginate(cluster=self.cluster,
                            serviceName=self.name, desiredStatus=desired_status):
                        old_tasks.update(page["taskArns"])
                        if desired_status == "RUNNING":
                            remaining.extend(page["taskArns"])
                active = []
                task_ids = sorted(old_tasks)
                for offset in range(0, len(task_ids), 100):
                    details = ecs.describe_tasks(cluster=self.cluster, tasks=task_ids[offset:offset+100])
                    if details.get("failures"):
                        raise RuntimeError("Cannot verify every legacy task stopped; service remains in maintenance")
                    active.extend(t for t in details["tasks"] if t["lastStatus"] != "STOPPED")
                stopped_service = ecs.describe_services(cluster=self.cluster, services=[self.name])["services"][0]
                if not active and not remaining and not stopped_service["runningCount"] and not stopped_service["pendingCount"]:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Earlier-schema tasks did not fully stop; service remains in maintenance. Inspect before retrying")
            print(json.dumps({"company_cutover": "legacy_tasks_stopped"}), flush=True)
        services = ecs.describe_services(cluster=self.cluster, services=[self.name])["services"]
        if release_only and (len(services) != 1 or services[0]["status"] != "ACTIVE"
                or len(services[0]["deployments"]) != 1
                or services[0]["deployments"][0].get("rolloutState") != "COMPLETED"):
            raise RuntimeError("Service changed during release preparation; coordinate and inspect before retrying")
        if services and services[0]["status"] != "INACTIVE":
            ecs.update_service(cluster=self.cluster, service=self.name, taskDefinition=task["taskDefinitionArn"], desiredCount=1,
                # The first schema cutover must not bootstrap the drained legacy task set
                # to satisfy a 100% minimum when desiredCount returns from zero.
                deploymentConfiguration={"maximumPercent": 200, "minimumHealthyPercent": 0 if company_cutover else 100, "deploymentCircuitBreaker": {"enable": True, "rollback": allow_rollback}})
        else:
            ecs.create_service(cluster=self.cluster, serviceName=self.name, taskDefinition=task["taskDefinitionArn"], desiredCount=1, launchType="FARGATE",
                platformVersion="1.4.0", healthCheckGracePeriodSeconds=60,
                networkConfiguration={"awsvpcConfiguration": {"subnets": self.subnets, "securityGroups": [self.state["security_group"]], "assignPublicIp": "DISABLED"}},
                loadBalancers=[{"targetGroupArn": self.state["target_group"], "containerName": "app", "containerPort": 8080}],
                deploymentConfiguration={"maximumPercent": 200, "minimumHealthyPercent": 100, "deploymentCircuitBreaker": {"enable": True, "rollback": False}}, tags=self.lower_tags)
        self.record(service=self.name, deployed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        print(json.dumps({"deployment_started": self.state["task_definition"], "url": self.state["url"]}))

    def status(self):
        result = {"environment": self.config.name, "url": self.state.get("url"), "prepared_revision": self.state.get("revision")}
        if self.state.get("database_ready"):
            result["database_init"] = {"status": "verified", "database": self.db_name}
        elif self.state.get("db_init_task"):
            tasks = self.client("ecs").describe_tasks(cluster=self.cluster, tasks=[self.state["db_init_task"]])["tasks"]
            if tasks:
                task = tasks[0]
                result["database_init"] = {"status": task["lastStatus"], "reason": task.get("stoppedReason"), "containers": [{"name": c["name"], "exit_code": c.get("exitCode"), "reason": c.get("reason")} for c in task["containers"]]}
                if task["lastStatus"] == "STOPPED" and all(c.get("exitCode") == 0 for c in task["containers"]):
                    result["database_init"]["next_step"] = "Run database-ready to revoke bootstrap secret access and record readiness"
            else:
                result["database_init"] = {"status": "unknown", "detail": "ECS no longer retains the initialization task; database is not marked verified"}
        if self.state.get("build_id"):
            b = self.client("codebuild").batch_get_builds(ids=[self.state["build_id"]])["builds"][0]
            result["build"] = {"status": b["buildStatus"], "phase": b.get("currentPhase"), "logs": b.get("logs", {}).get("deepLink")}
        if self.state.get("service"):
            s = self.client("ecs").describe_services(cluster=self.cluster, services=[self.name])["services"][0]
            task = self.client("ecs").describe_task_definition(taskDefinition=s["taskDefinition"])["taskDefinition"]
            app = next(c for c in task["containerDefinitions"] if c["name"] == "app")
            deployed_env = {entry["name"]: entry["value"] for entry in app.get("environment", [])}
            result["service"] = {"running": s["runningCount"], "pending": s["pendingCount"],
                "task_definition": s["taskDefinition"], "target_revision": deployed_env.get("DEPLOY_REVISION"),
                "target_image": app["image"], "deployments": [{"task_definition": d["taskDefinition"],
                    "state": d.get("rolloutState"), "running": d["runningCount"], "pending": d["pendingCount"]} for d in s["deployments"]],
                "events": [{"at": str(e["createdAt"]), "message": e["message"]} for e in s["events"][:3]]}
            result["targets"] = self.client("elbv2").describe_target_health(TargetGroupArn=self.state["target_group"])["TargetHealthDescriptions"]
        if self.state.get("distribution"):
            result["cloudfront"] = self.client("cloudfront").get_distribution(Id=self.state["distribution"])["Distribution"]["Status"]
        print(json.dumps(result, indent=2, default=str))

    def database_ready(self):
        tasks = self.client("ecs").describe_tasks(cluster=self.cluster, tasks=[self.state["db_init_task"]])["tasks"]
        if len(tasks) != 1 or tasks[0]["lastStatus"] != "STOPPED" or not tasks[0]["containers"] or any(c.get("exitCode") != 0 for c in tasks[0]["containers"]):
            raise RuntimeError("Database initialization has not completed successfully")
        ensure_call(self.client("iam").delete_role_policy, {"NoSuchEntity"}, RoleName=f"{self.name}-db-bootstrap", PolicyName="bootstrap-secrets")
        self.record(database_ready=True)
        print(json.dumps({"environment": self.config.name, "database": self.db_name, "database_ready": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "migrate-legacy-dev", "prepare", "database", "database-ready", "build", "deploy", "status", "promotion-manifest"])
    parser.add_argument("--environment", choices=["dev", "prod"], required=True)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--fleet-env", type=Path, default=ROOT / ".env.fleet")
    parser.add_argument("--revision", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--promotion-manifest", type=Path)
    parser.add_argument("--reviewed-by")
    parser.add_argument("--review-reference")
    parser.add_argument("--release-only", action="store_true", help="Build/deploy using existing resources; no IAM, bucket, project or service creation")
    parser.add_argument("--company-cutover", action="store_true", help="First company upgrade only: stop every earlier-schema task before starting company accounts")
    args = parser.parse_args()
    if args.release_only and args.command not in ("build", "deploy"):
        parser.error("--release-only is only valid for build or deploy")
    if args.company_cutover and args.command != "deploy":
        parser.error("--company-cutover is only valid for deploy")
    config = load_environment(args.environment)
    if args.command == "plan":
        print(json.dumps({"configuration": config.raw, "state_path": str(config.state_path), "credentials_printed": False}, indent=2))
        return
    if args.command == "migrate-legacy-dev":
        if args.environment != "dev":
            parser.error("Legacy state can only be explicitly copied into dev")
        print(json.dumps(migrate_legacy_dev(), indent=2))
        return
    if args.environment == "dev" and not config.state_path.exists() and (ROOT / ".local" / "mvp" / "aws.json").exists():
        parser.error("Run migrate-legacy-dev --environment dev before using the existing shared development deployment")
    mutation = args.command in {"prepare", "database", "database-ready", "build", "deploy"}
    if mutation and not config["enabled"]:
        parser.error("This environment is disabled pending reviewed configuration")
    if args.command == "promotion-manifest" and (not args.reviewed_by or not args.review_reference):
        parser.error("promotion-manifest requires --reviewed-by and --review-reference")
    if args.environment == "prod" and args.command == "deploy" and not args.promotion_manifest:
        parser.error("Production deployment requires --promotion-manifest")
    deployment = Deployment(args.profile, args.environment)
    if mutation:
        deployment.guard_resources()
    if args.command == "prepare":
        deployment.prepare(args.fleet_env)
    elif args.command == "database":
        deployment.database()
    elif args.command == "database-ready":
        deployment.database_ready()
    elif args.command == "build":
        deployment.build(args.revision, args.release_only)
    elif args.command == "deploy":
        deployment.deploy(args.promotion_manifest, args.release_only, args.company_cutover)
    elif args.command == "promotion-manifest":
        deployment.promotion_manifest(args.revision, args.reviewed_by, args.review_reference)
    else:
        deployment.status()


if __name__ == "__main__":
    main()
