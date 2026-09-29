# Shared development and future production

Development remains one shared website and one PostgreSQL database. Per-person users, workspaces, membership, sessions, and assigned workers live in that database; a new contributor does not receive another application deployment. Production has a separate logical database and login role, application secret, ECS service/task family, application/execution IAM roles, security group, target group, CloudFront distribution, report bucket, sessions, and worker assignments. The current configuration proposes reusing the account, VPC, ALB and Aurora cluster: this is logical environment separation, not separate AWS accounts or database compute.

[`config/environments/dev.json`](../config/environments/dev.json) deliberately retains the deployed `ctrlloop-mvp` identities and database `ctrlloop_mvp`. Renaming resources would create a second development environment or lose the link to its saved history. [`config/environments/prod.json`](../config/environments/prod.json) reserves distinct `ctrlloop-prod` / `ctrlloop_prod` identities and is **disabled**. No production resources were created by this change.

## Domain verification

A read-only AWS inspection at **2026-09-29 00:18 UTC** authenticated account `686458136219` through profile `mangorts-dev`. No hosted zone, Route53 registrar entry, matching ACM certificate in `us-east-1` or `us-west-1`, or CloudFront alias was found for the exact requested **`controlloop.io`** or **`dev.controlloop.io`**. This establishes absence from the inspected account; it does not establish that the user cannot control the domain through another registrar/account.

The user subsequently selected **`dev.ctrlloop.io`** explicitly. Public DNS checked on **2026-09-29** delegates `ctrlloop.io` to the four nameservers of the account's public Route53 zone `Z0430279RQF1HYJD8WY7`; the new dev name initially had no record. The apex and `www` landing page use distribution `E20QAN5SEVBNDT` and must remain there. The development application uses the separate existing distribution `EFZQEEWLIM0FZ`. Its current development origin remains **`https://d1t0sm9jhdinfi.cloudfront.net`** until custom-domain verification succeeds. Production's domain is still an unverified placeholder and has not been changed. The initial metadata-only audit is ignored at `.local/mvp/domain-environment-audit.json`.

The authorized custom-domain setup is pending renewed AWS SSO authentication. Its sequence is to request/reuse a certificate covering the exact dev hostname in **us-east-1**, install ACM's validation CNAME in the verified zone, and wait for `ISSUED`. Update the existing app distribution with its current ETag, changing only its alias and viewer certificate; wait for `Deployed`, then publish the dev Route53 alias. Check public DNS and hostname-verified HTTPS before updating the environment's `expected_url`, its local `aws.json` URL, and deploying the corresponding `APP_ORIGIN`. No certificate, DNS or CloudFront mutation has happened yet. These steps preserve the apex distribution and the existing app origin settings. [AWS CloudFront certificate requirements](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html), [Route53 CloudFront alias requirements](https://docs.aws.amazon.com/Route53/latest/APIReference/API_AliasTarget.html).

## Configuration and local state

All deployment commands require `--environment dev` or `--environment prod`; there is no implicit production default. [`scripts/deployment_config.py`](../scripts/deployment_config.py) provides `load_environment(name)` with `.state_dir`, `.state_path`, `.secret_path`, `.worker_state_path`, `.worker_dir(username)`, `.resource_name`, and the non-secret `.raw` configuration. Worker/admin tools share this module.

Environment state lives under ignored `.local/environments/{environment}/`. The explicit migration below copies `aws.json`, `deployment-secrets.json`, `worker.json`, and `access.md` from `.local/mvp/` into dev only. Existing destination files are never overwritten; legacy files are never moved or deleted. The migration was performed during this change. Historical evidence stays in `.local/mvp/`. Do not copy dev credentials or database state into prod.

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py migrate-legacy-dev --environment dev
.venv/Scripts/python.exe scripts/deploy_aws.py plan --environment dev
.venv/Scripts/python.exe scripts/deploy_aws.py status --environment dev --profile mangorts-dev
```

`plan` reads only local non-secret configuration. `status` reads AWS without changing its resources or local readiness; initial database completion is now a separate `database-ready` command. Account, region, secret name, database/user, service/task family, repository, role names, bucket names and stored resource identifiers are checked against the selected environment. Before writes, live checks verify the configured subnets, security groups, ALB listener, database endpoint and ECS cluster. The source revision is restricted to a safe immutable image tag. A mismatched copied state is rejected.

## First development upgrade

The first upgraded app boot receives `APP_ENV=dev`, `BOOTSTRAP_LEGACY_DEV=true`, owner username `owner`, display name `Owner`, workspace slug `ctrlloop-development`, and name `CTRL Loop Development`. The existing `APP_PASSWORD`, `WORKER_TOKEN`, `WORKER_ID`, `FLEET_NAMESPACE`, `FLEET_SANDBOX`, and `FLEET_CLAIM=omarchy-trial` seed the legacy owner/workspace/worker once. Runtime ownership and worker routing then come from database records. These bootstrap settings do not create a shared runtime password/token fallback. Production forbids the legacy bootstrap and starts with explicit administrative provisioning of members and assigned workers.

The ordinary app requires `DATABASE_URL`, `APP_ORIGIN`, `OPENAI_API_KEY`, and Fleet client credentials; report storage additionally uses its environment's `REPORT_BUCKET` and task role. No OpenAI key or Fleet service secret is placed in a worker or checked-in configuration. The production OpenAI secret reference is reserved separately and must exist before enabling production.

The existing dev database and resources are already prepared; do not reinitialize them. The first identity cutover needs a short maintenance window. Keep instruction generation and runs idle, run the explicit backup/migration from the [administrator guide](developer-onboarding.md#administrator-migration-and-assignment), and keep new work paused until the new service is verified. Migration takes a consistent snapshot under exclusive locks and makes ownership columns required before committing; old app instances then reject new session/workflow/run inserts.

From the authorized administrator session, build and deploy the coordinated first upgrade:

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py build --environment dev --profile mangorts-dev --revision REVIEWED-UNIQUE-REVISION
.venv/Scripts/python.exe scripts/deploy_aws.py status --environment dev --profile mangorts-dev
.venv/Scripts/python.exe scripts/deploy_aws.py deploy --environment dev --profile mangorts-dev --identity-cutover
.venv/Scripts/python.exe scripts/deploy_aws.py status --environment dev --profile mangorts-dev
```

Wait for a successful build before deploy. `--identity-cutover` is required only while replacing the legacy development app: it scales the service to zero, waits for every old task and desktop connection to terminate, then starts the new task. A normal overlapping rollout would allow the old app to accept a new member's session without workspace checks. Automatic rollback is disabled until a stable current deployment with the matching `APP_ENV` is verified; a failed first cutover must be recovered with identity-aware code, never the legacy image. The narrow release command refuses failed/in-progress rollouts; an administrator must inspect and recover those explicitly.

Completion requires one healthy running task, no pending tasks, the intended image digest, healthy target, and application/authentication checks. Activate Kevin only after those checks. Later releases omit `--identity-cutover` and use the release-only commands in the onboarding guide. Source uploads use a hash-bound key and conditional creation; each build names that exact source, and deploy checks its revision/hash binding. Shared development deployments must be coordinated serially; local state files are not a distributed deployment lock. Creating a fresh approved environment uses `prepare`, `database`, then `database-ready` after successful initialization. Bootstrap database-secret access is removed only by that explicit completion command.

## Explicit production promotion

Production is disabled until its account/resource bindings, separate provider secret, domain access, initial database and member/worker provisioning have been reviewed. Enabling the configuration is an operator decision; the script does not provision production in response to a development command. It refuses `build --environment prod`. Production uses the exact immutable image already built and reviewed in dev, so it does not rebuild different source during promotion.

After the dev deployment is healthy and the named revision has been reviewed, create a local review record:

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py promotion-manifest --environment dev --profile mangorts-dev --revision REVIEWED-UNIQUE-REVISION --reviewed-by REVIEWER --review-reference REVIEW-URL-OR-RECORD
```

This requires a stable current dev service and healthy targets. The manifest records account/region, exact source hash, revision, successful CodeBuild ID, immutable ECR image digest, dev task definition, reviewer, review reference and timestamp. The reviewer fields are an explicit operator attestation, not a cryptographic approval service. Existing review files are not overwritten. The command never deploys production.

Only after production has been explicitly enabled and prepared can an operator invoke:

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py deploy --environment prod --profile APPROVED-PROD-PROFILE --promotion-manifest .local/environments/dev/promotions/REVIEWED-UNIQUE-REVISION.json
```

Promotion rechecks manifest identity, successful source build and revision, registry digest, and that the referenced dev task definition ran that exact image. It supplies production database/provider secrets and roles, with `APP_ENV=prod` and `BOOTSTRAP_LEGACY_DEV=false`. It does not copy sessions, membership, passwords, worker credentials, assignments, run rows or reports from dev. The dev ECR artifact is shared read-only by digest; application state and report storage are separate. This path has been compiled and reviewed but has not been exercised against a production deployment.

## Kevin's development deployment access

GitHub collaborator `kevin-chengchihlee` has repository write access, as separately verified by the implementation pass. That does not establish an AWS IAM Identity Center identity or grant cloud access. No IAM role, SSO assignment, GitHub OIDC trust or CI workflow was created here.

An exact-username lookup in the account's existing Identity Center instance found neither `kevin` nor `kevin-chengchihlee`. The remaining input is Kevin's actual AWS identity, or the administrator-approved identity to create; a GitHub username is not silently turned into an AWS account.

The owner explicitly requested unrestricted provider access for Kevin. Assign his personal IAM Identity Center user the existing **AdministratorAccess** permission set for account `686458136219`, following the [AWS identity setup guide](developer-onboarding.md#aws-identities-and-full-account-access). The owner's existing `ScoobyXD` identity already has that permission set. The earlier proposal for a restricted release role is superseded; no restrictive permission set is required or being created.

The `build --release-only` and `deploy --release-only` commands remain useful with administrator credentials: they use existing resources and skip IAM policy changes, bucket configuration, CodeBuild project mutation and service creation. Environment/resource checks prevent accidentally selecting the wrong target regardless of the operator's provider permissions. The unrestricted operator paths perform infrastructure provisioning. Account-wide provider access is separate from each signed-in website member's assigned desktop.

A future GitHub `workflow_dispatch` path should use an exact repository/environment OIDC trust and explicit reviewed revision, with independent production approval/trust. It remains deferred; merging or pushing code currently deploys neither environment.
