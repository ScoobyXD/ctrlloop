"""Deploy CTRL Loop's isolated MVP service into the existing development VPC.

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
import secrets
import time
from urllib.parse import quote
import zipfile

import boto3
from botocore.exceptions import ClientError
ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local" / "mvp"
STATE_PATH = LOCAL / "aws.json"
SECRET_PATH = LOCAL / "deployment-secrets.json"
NAME = "ctrlloop-mvp"
ACCOUNT = "686458136219"
REGION = "us-west-1"
VPC = "vpc-091ee99d4afb8bec5"
SUBNETS = ["subnet-0c96aa9cdb41d46f7", "subnet-0d7cdf9a8970301c1"]
ALB_SG = "sg-080dc70b173bd3d6c"
DB_SG = "sg-085727cf5320e53ac"
LISTENER = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/mangorts-dev-api/e34cd9745d895b7c/bf88b3739c743fca"
DB_HOST = "miraerts-northerncalifornia-devcluster.cluster-c3mk8yoeah0y.us-west-1.rds.amazonaws.com"
DB_NAME = "ctrlloop_mvp"
DB_USER = "ctrlloop_mvp"
CLUSTER = "mangorts-dev-cluster"
OPENAI_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mangorts/dev/openai-api-key-EaABNe"
TAGS = [{"Key": "Project", "Value": "ctrlloop"}, {"Key": "Environment", "Value": "mvp"}]
LOWER_TAGS = [{"key": t["Key"], "value": t["Value"]} for t in TAGS]


def save(path, value):
    LOCAL.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


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
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() in {"CUA_CLIENT_ID", "CUA_CLIENT_SECRET"}:
            values[name.strip()] = value.strip().strip("\"'")
    if set(values) != {"CUA_CLIENT_ID", "CUA_CLIENT_SECRET"}:
        raise RuntimeError("Fleet credential file is missing required values")
    return values


class Deployment:
    def __init__(self, profile):
        self.session = boto3.Session(profile_name=profile, region_name=REGION)
        actual = self.session.client("sts").get_caller_identity()["Account"]
        if actual != ACCOUNT:
            raise RuntimeError("Refusing deployment to an unexpected AWS account")
        self.state = load(STATE_PATH)
        self.secret = load(SECRET_PATH)

    def client(self, name):
        return self.session.client(name)

    def record(self, **kwargs):
        self.state.update(kwargs)
        save(STATE_PATH, self.state)

    def prepare(self, fleet_env):
        sm = self.client("secretsmanager")
        if not self.secret:
            self.secret = {
                "APP_PASSWORD": secrets.token_urlsafe(32),
                "SESSION_SECRET": secrets.token_urlsafe(32),
                "WORKER_TOKEN": secrets.token_urlsafe(48),
                "DATABASE_PASSWORD": secrets.token_urlsafe(36),
                "ORIGIN_VERIFY": secrets.token_urlsafe(36),
                **fleet_credentials(fleet_env),
            }
            self.secret["DATABASE_URL"] = (
                f"postgresql://{DB_USER}:{quote(self.secret['DATABASE_PASSWORD'], safe='')}"
                f"@{DB_HOST}:5432/{DB_NAME}?sslmode=require"
            )
            save(SECRET_PATH, self.secret)
        if "app_secret_arn" not in self.state:
            try:
                result = sm.create_secret(Name="ctrlloop/mvp/app", SecretString=json.dumps(self.secret), Tags=TAGS)
            except sm.exceptions.ResourceExistsException:
                # Recover existing credentials rather than rotating an active deployment.
                result = sm.get_secret_value(SecretId="ctrlloop/mvp/app")
                self.secret = json.loads(result["SecretString"])
                save(SECRET_PATH, self.secret)
            self.record(app_secret_arn=result["ARN"])
        self.network()
        self.execution_role()
        self.edge()
        ecr = self.client("ecr")
        try:
            repo = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]
        except ecr.exceptions.RepositoryNotFoundException:
            repo = ecr.create_repository(repositoryName=NAME, imageScanningConfiguration={"scanOnPush": True},
                                         imageTagMutability="IMMUTABLE", tags=TAGS)["repository"]
        self.record(repository=repo["repositoryUri"])
        logs = self.client("logs")
        ensure_call(logs.create_log_group, {"ResourceAlreadyExistsException"}, logGroupName=f"/ecs/{NAME}")
        logs.put_retention_policy(logGroupName=f"/ecs/{NAME}", retentionInDays=14)
        print(json.dumps({"prepared": True, "url": self.state["url"], "state": str(STATE_PATH)}))

    def database(self):
        """Start a one-off initializer inside the VPC, with no public DB ingress."""
        iam = self.client("iam")
        role_name = f"{NAME}-db-bootstrap"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS)
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
        admin_secret = self.client("secretsmanager").describe_secret(SecretId="mangorts/dev/db-password")["ARN"]
        iam.put_role_policy(RoleName=role_name, PolicyName="bootstrap-secrets", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [self.state["app_secret_arn"], admin_secret]}]}))
        init_code = '''import os
import psycopg
from psycopg import sql
user = os.environ["DB_USER"]
database_name = os.environ["DB_NAME"]
with psycopg.connect(host=os.environ["DB_HOST"], dbname="postgres", user="ScoobyXD", password=os.environ["DB_ADMIN_PASSWORD"], sslmode="require", connect_timeout=20, autocommit=True) as conn:
    if not conn.execute("SELECT rolname FROM pg_roles WHERE rolname=%s", (user,)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(user), sql.Literal(os.environ["DB_PASSWORD"])))
    database = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (database_name,)).fetchone()
    if not database:
        conn.execute(sql.SQL("GRANT {} TO {} WITH SET TRUE").format(sql.Identifier(user), sql.Identifier("ScoobyXD")))
        try:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database_name), sql.Identifier(user)))
        finally:
            conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(user), sql.Identifier("ScoobyXD")))
    elif database[0] != user:
        raise RuntimeError("Existing CTRL Loop database has an unexpected owner")
with psycopg.connect(host=os.environ["DB_HOST"], dbname=database_name, user=user, password=os.environ["DB_PASSWORD"], sslmode="require", connect_timeout=20) as conn:
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database_name)))
    conn.execute("SELECT 1")
print("CTRL Loop dedicated database created and login verified")
'''
        ecs = self.client("ecs")
        task = ecs.register_task_definition(family=f"{NAME}-db-init", networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="512",
            executionRoleArn=iam.get_role(RoleName=role_name)["Role"]["Arn"],
            containerDefinitions=[{"name": "init", "image": "public.ecr.aws/docker/library/python:3.12-slim", "essential": True,
                "entryPoint": ["sh", "-c"], "command": ["pip install --quiet 'psycopg[binary]==3.3.6' && python -c \"$INIT_CODE\""],
                "environment": [{"name": "INIT_CODE", "value": init_code}, {"name": "DB_USER", "value": DB_USER}, {"name": "DB_NAME", "value": DB_NAME}, {"name": "DB_HOST", "value": DB_HOST}],
                "secrets": [{"name": "DB_ADMIN_PASSWORD", "valueFrom": admin_secret}, {"name": "DB_PASSWORD", "valueFrom": f"{self.state['app_secret_arn']}:DATABASE_PASSWORD::"}],
                "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": f"/ecs/{NAME}", "awslogs-region": REGION, "awslogs-stream-prefix": "bootstrap"}}}], tags=LOWER_TAGS)["taskDefinition"]
        result = after_iam_propagation(ecs.run_task, cluster=CLUSTER, taskDefinition=task["taskDefinitionArn"], launchType="FARGATE", platformVersion="1.4.0",
            networkConfiguration={"awsvpcConfiguration": {"subnets": SUBNETS, "securityGroups": [self.state["security_group"]], "assignPublicIp": "DISABLED"}}, tags=LOWER_TAGS)
        if result.get("failures"):
            raise RuntimeError(f"DB init task launch failed: {result['failures']}")
        self.record(db_init_task=result["tasks"][0]["taskArn"], database=DB_NAME, database_host=DB_HOST, database_ready=False)
        print(json.dumps({"database_init_started": self.state["db_init_task"]}))

    def network(self):
        ec2 = self.client("ec2")
        groups = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [VPC]},
                                                       {"Name": "group-name", "Values": [NAME]}])["SecurityGroups"]
        group_id = groups[0]["GroupId"] if groups else ec2.create_security_group(
            GroupName=NAME, Description="CTRL Loop MVP service; ingress only from existing ALB", VpcId=VPC,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
        self.record(security_group=group_id)
        ensure_call(ec2.authorize_security_group_ingress, {"InvalidPermission.Duplicate"}, GroupId=group_id,
                    IpPermissions=[{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
                                    "UserIdGroupPairs": [{"GroupId": ALB_SG, "Description": "CTRL Loop from shared ALB"}]}])
        ensure_call(ec2.authorize_security_group_ingress, {"InvalidPermission.Duplicate"}, GroupId=DB_SG,
                    IpPermissions=[{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                                    "UserIdGroupPairs": [{"GroupId": group_id, "Description": "CTRL Loop dedicated database role"}]}])
        elb = self.client("elbv2")
        try:
            group = elb.describe_target_groups(Names=[NAME])["TargetGroups"][0]
        except elb.exceptions.TargetGroupNotFoundException:
            group = elb.create_target_group(Name=NAME, Protocol="HTTP", Port=8080, VpcId=VPC, TargetType="ip",
                HealthCheckPath="/healthz", HealthCheckProtocol="HTTP", HealthCheckIntervalSeconds=15,
                HealthCheckTimeoutSeconds=5, HealthyThresholdCount=2, UnhealthyThresholdCount=3,
                Matcher={"HttpCode": "200"}, Tags=TAGS)["TargetGroups"][0]
        self.record(target_group=group["TargetGroupArn"])
        elb.modify_target_group_attributes(TargetGroupArn=group["TargetGroupArn"],
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}])
        if "listener_rule" not in self.state:
            rules = elb.describe_rules(ListenerArn=LISTENER)["Rules"]
            existing = next((r for r in rules if any(a.get("TargetGroupArn") == group["TargetGroupArn"] for a in r["Actions"])), None)
            if existing:
                rule = existing
            else:
                priorities = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
                priority = next(p for p in range(110, 200) if p not in priorities)
                rule = elb.create_rule(ListenerArn=LISTENER, Priority=priority,
                    Conditions=[{"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "x-ctrlloop-origin",
                        "Values": [self.secret["ORIGIN_VERIFY"]]}}],
                    Actions=[{"Type": "forward", "TargetGroupArn": group["TargetGroupArn"]}], Tags=TAGS)["Rules"][0]
            self.record(listener_rule=rule["RuleArn"])

    def execution_role(self):
        iam = self.client("iam")
        role_name = f"{NAME}-execution"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS)
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
        iam.put_role_policy(RoleName=role_name, PolicyName="ctrl-loop-secrets", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [self.state["app_secret_arn"], OPENAI_SECRET]}]}))
        self.record(execution_role=iam.get_role(RoleName=role_name)["Role"]["Arn"])

    def edge(self):
        cf = self.client("cloudfront")
        if "distribution" not in self.state:
            if "origin_policy" not in self.state:
                origin_policy = cf.create_origin_request_policy(OriginRequestPolicyConfig={
                    "Name": f"{NAME}-headers-{int(time.time())}", "Comment": "Session cookies and WebSocket headers; origin host retained",
                    "HeadersConfig": {"HeaderBehavior": "allExcept", "Headers": {"Quantity": 1, "Items": ["host"]}},
                    "CookiesConfig": {"CookieBehavior": "all"}, "QueryStringsConfig": {"QueryStringBehavior": "all"}})
                self.record(origin_policy=origin_policy["OriginRequestPolicy"]["Id"])
            dist = cf.create_distribution_with_tags(DistributionConfigWithTags={"Tags": {"Items": TAGS}, "DistributionConfig": {
                "CallerReference": f"{NAME}-{int(time.time())}", "Comment": "CTRL Loop MVP authenticated application and native desktop viewer",
                "Enabled": True, "PriceClass": "PriceClass_100", "HttpVersion": "http2", "IsIPV6Enabled": True,
                "Origins": {"Quantity": 1, "Items": [{"Id": NAME, "DomainName": "origin.dev.mangorts.com",
                    "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "x-ctrlloop-origin", "HeaderValue": self.secret["ORIGIN_VERIFY"]}]},
                    "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}, "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}}]},
                "DefaultCacheBehavior": {"TargetOriginId": NAME, "ViewerProtocolPolicy": "redirect-to-https",
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

    def build(self, revision):
        """Use managed ephemeral CodeBuild so local Docker/WSL is not required."""
        s3 = self.client("s3")
        bucket = f"{NAME}-build-{ACCOUNT}-{REGION}"
        ensure_call(s3.create_bucket, {"BucketAlreadyOwnedByYou"}, Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={k: True for k in [
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"]})
        s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
            {"ID": "expire-build-sources", "Status": "Enabled", "Filter": {"Prefix": "source/"}, "Expiration": {"Days": 7}}]})
        logs = self.client("logs")
        ensure_call(logs.create_log_group, {"ResourceAlreadyExistsException"}, logGroupName=f"/aws/codebuild/{NAME}")
        logs.put_retention_policy(logGroupName=f"/aws/codebuild/{NAME}", retentionInDays=14)
        archive = LOCAL / f"source-{revision}.zip"
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
        key = f"source/{revision}.zip"
        s3.upload_file(str(archive), bucket, key)
        iam = self.client("iam")
        role_name = f"{NAME}-build"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        ensure_call(iam.create_role, {"EntityAlreadyExists"}, RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS)
        iam.put_role_policy(RoleName=role_name, PolicyName="build-image", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/source/*"},
            {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": f"arn:aws:s3:::{bucket}"},
            {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{NAME}"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/codebuild/{NAME}*"},
        ]}))
        repo = self.state["repository"]
        buildspec = f"""version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region {REGION} | docker login --username AWS --password-stdin {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com
  build:
    commands:
      - docker build --platform linux/amd64 -t {repo}:{revision} .
  post_build:
    commands:
      - docker push {repo}:{revision}
"""
        config = {"name": NAME, "source": {"type": "S3", "location": f"{bucket}/{key}", "buildspec": buildspec},
            "artifacts": {"type": "NO_ARTIFACTS"}, "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True}, "serviceRole": iam.get_role(RoleName=role_name)["Role"]["Arn"],
            "timeoutInMinutes": 20, "queuedTimeoutInMinutes": 10}
        cb = self.client("codebuild")
        if cb.batch_get_projects(names=[NAME])["projects"]:
            cb.update_project(**config)
        else:
            after_iam_propagation(cb.create_project, **config, tags=LOWER_TAGS)
        result = cb.start_build(projectName=NAME)
        self.record(build_id=result["build"]["id"], revision=revision, image=f"{repo}:{revision}", build_source_bucket=bucket,
                    source_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        print(json.dumps({"build_started": self.state["build_id"], "revision": revision}))

    def deploy(self):
        if not self.state.get("database_ready"):
            raise RuntimeError("Database initialization is not verified. Run status after it finishes.")
        build = self.client("codebuild").batch_get_builds(ids=[self.state["build_id"]])["builds"][0]
        if build["buildStatus"] != "SUCCEEDED":
            raise RuntimeError("Refusing deployment until the current image build succeeds")
        image = self.client("ecr").describe_images(repositoryName=NAME, imageIds=[{"imageTag": self.state["revision"]}])["imageDetails"][0]
        self.record(image_digest=image["imageDigest"])
        ecs = self.client("ecs")
        env = {"OPENAI_MODEL": "gpt-6-luna", "APP_ORIGIN": self.state["url"],
               "WORKER_ID": "ctrlloop-omarchy-20260927-1708-e33048ae", "FLEET_NAMESPACE": "ctrlloop-omarchy-20260927-1708",
               "FLEET_SANDBOX": "ctrlloop-omarchy-20260927-1708-e33048ae", "DEPLOY_REVISION": self.state["revision"]}
        secret_names = ["DATABASE_URL", "APP_PASSWORD", "SESSION_SECRET", "WORKER_TOKEN", "CUA_CLIENT_ID", "CUA_CLIENT_SECRET"]
        task = ecs.register_task_definition(family=NAME, networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="1024",
            executionRoleArn=self.state["execution_role"], runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            containerDefinitions=[{"name": "app", "image": f"{self.state['repository']}@{self.state['image_digest']}", "essential": True,
                "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                "environment": [{"name": k, "value": v} for k, v in env.items()],
                "secrets": [{"name": k, "valueFrom": f"{self.state['app_secret_arn']}:{k}::"} for k in secret_names] + [{"name": "OPENAI_API_KEY", "valueFrom": OPENAI_SECRET}],
                "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": f"/ecs/{NAME}", "awslogs-region": REGION, "awslogs-stream-prefix": "app"}}}],
            tags=LOWER_TAGS)["taskDefinition"]
        self.record(task_definition=task["taskDefinitionArn"])
        services = ecs.describe_services(cluster=CLUSTER, services=[NAME])["services"]
        if services and services[0]["status"] != "INACTIVE":
            ecs.update_service(cluster=CLUSTER, service=NAME, taskDefinition=task["taskDefinitionArn"], desiredCount=1,
                deploymentConfiguration={"maximumPercent": 200, "minimumHealthyPercent": 100, "deploymentCircuitBreaker": {"enable": True, "rollback": True}})
        else:
            ecs.create_service(cluster=CLUSTER, serviceName=NAME, taskDefinition=task["taskDefinitionArn"], desiredCount=1, launchType="FARGATE",
                platformVersion="1.4.0", healthCheckGracePeriodSeconds=60,
                networkConfiguration={"awsvpcConfiguration": {"subnets": SUBNETS, "securityGroups": [self.state["security_group"]], "assignPublicIp": "DISABLED"}},
                loadBalancers=[{"targetGroupArn": self.state["target_group"], "containerName": "app", "containerPort": 8080}],
                deploymentConfiguration={"maximumPercent": 200, "minimumHealthyPercent": 100, "deploymentCircuitBreaker": {"enable": True, "rollback": True}}, tags=LOWER_TAGS)
        self.record(service=NAME, deployed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        print(json.dumps({"deployment_started": self.state["task_definition"], "url": self.state["url"]}))

    def status(self):
        result = {"url": self.state.get("url"), "revision": self.state.get("revision")}
        if self.state.get("database_ready"):
            result["database_init"] = {"status": "verified", "database": DB_NAME}
        elif self.state.get("db_init_task"):
            tasks = self.client("ecs").describe_tasks(cluster=CLUSTER, tasks=[self.state["db_init_task"]])["tasks"]
            if tasks:
                task = tasks[0]
                result["database_init"] = {"status": task["lastStatus"], "reason": task.get("stoppedReason"), "containers": [{"name": c["name"], "exit_code": c.get("exitCode"), "reason": c.get("reason")} for c in task["containers"]]}
                if task["lastStatus"] == "STOPPED" and all(c.get("exitCode") == 0 for c in task["containers"]):
                    ensure_call(self.client("iam").delete_role_policy, {"NoSuchEntity"}, RoleName=f"{NAME}-db-bootstrap", PolicyName="bootstrap-secrets")
                    self.record(database_ready=True)
            else:
                result["database_init"] = {"status": "unknown", "detail": "ECS no longer retains the initialization task; database is not marked verified"}
        if self.state.get("build_id"):
            b = self.client("codebuild").batch_get_builds(ids=[self.state["build_id"]])["builds"][0]
            result["build"] = {"status": b["buildStatus"], "phase": b.get("currentPhase"), "logs": b.get("logs", {}).get("deepLink")}
        if self.state.get("service"):
            s = self.client("ecs").describe_services(cluster=CLUSTER, services=[NAME])["services"][0]
            result["service"] = {"running": s["runningCount"], "pending": s["pendingCount"], "events": [{"at": str(e["createdAt"]), "message": e["message"]} for e in s["events"][:3]]}
            result["targets"] = self.client("elbv2").describe_target_health(TargetGroupArn=self.state["target_group"])["TargetHealthDescriptions"]
        if self.state.get("distribution"):
            result["cloudfront"] = self.client("cloudfront").get_distribution(Id=self.state["distribution"])["Distribution"]["Status"]
        print(json.dumps(result, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "database", "build", "deploy", "status"])
    parser.add_argument("--profile", default="mangorts-dev")
    parser.add_argument("--fleet-env", type=Path, default=Path(r"C:\Users\Jonat\ctrlloop\.env.fleet"))
    parser.add_argument("--revision", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    deployment = Deployment(args.profile)
    if args.command == "prepare":
        deployment.prepare(args.fleet_env)
    elif args.command == "database":
        deployment.database()
    elif args.command == "build":
        deployment.build(args.revision)
    elif args.command == "deploy":
        deployment.deploy()
    else:
        deployment.status()


if __name__ == "__main__":
    main()
