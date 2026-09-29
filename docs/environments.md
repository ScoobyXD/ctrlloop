# Shared development and future production

Development remains one shared website and one PostgreSQL database. Per-person users, companies, sessions, and assigned workers live in that database; a new contributor does not receive another application deployment. Production has a separate logical database and login role, application secret, ECS service/task family, application/execution IAM roles, security group, target group, CloudFront distribution, report bucket, sessions, and worker assignments. The current configuration proposes reusing the account, VPC, ALB and Aurora cluster: this is logical environment separation, not separate AWS accounts or database compute.

[`config/environments/dev.json`](../config/environments/dev.json) deliberately retains the deployed `ctrlloop-mvp` identities and database `ctrlloop_mvp`. Renaming resources would create a second development environment or lose the link to its saved history. [`config/environments/prod.json`](../config/environments/prod.json) reserves distinct `ctrlloop-prod` / `ctrlloop_prod` identities and is **disabled**. No production resources were created by this change.

## Domain verification

The development configuration pins **`https://dev.ctrlloop.io`**, distribution `EFZQEEWLIM0FZ`, and the existing `ctrlloop-mvp` service/database identities. The apex and `www` landing page remain separately hosted. The company-account source change makes no DNS, certificate, CloudFront, application, or live database changes. Inspect deployed state and hostname-verified HTTPS independently before releasing. Production remains a disabled separate configuration; its domain/provider bindings need operator review.

## Configuration and local state

All deployment commands require `--environment dev` or `--environment prod`; there is no implicit production default. [`scripts/deployment_config.py`](../scripts/deployment_config.py) provides `load_environment(name)` with `.state_dir`, `.state_path`, `.secret_path`, `.worker_dir(email)`, `.resource_name`, and the non-secret `.raw` configuration. Worker/admin tools share this module.

Environment state lives under ignored `.local/environments/{environment}/`. The explicit migration below copies `aws.json`, `deployment-secrets.json`, `worker.json`, and `access.md` from `.local/mvp/` into dev only. Existing destination files are never overwritten; legacy files are never moved or deleted. This historical state copy is separate from the company schema migration. Historical evidence stays in `.local/mvp/`. Do not copy dev credentials or database state into prod.

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py migrate-legacy-dev --environment dev
.venv/Scripts/python.exe scripts/deploy_aws.py plan --environment dev
.venv/Scripts/python.exe scripts/deploy_aws.py status --environment dev --profile mangorts-dev
```

`plan` reads only local non-secret configuration. `status` reads AWS without changing its resources or local readiness; initial database completion is now a separate `database-ready` command. Account, region, secret name, database/user, service/task family, repository, role names, bucket names and stored resource identifiers are checked against the selected environment. Before writes, live checks verify the configured subnets, security groups, ALB listener, database endpoint and ECS cluster. The source revision is restricted to a safe immutable image tag. A mismatched copied state is rejected.

## First company upgrade

There is no runtime default company, owner, shared login password, or globally selected worker. All accounts, personal/company password hashes and per-user worker routing come from PostgreSQL. The former bootstrap settings are removed from current deployment configuration. Existing credentials are preserved by the explicit [company migration](company-accounts.md#migration-preflight); startup does not infer legacy ownership or migrate live data automatically.

The app needs `DATABASE_URL`, `APP_ORIGIN`, `OPENAI_API_KEY`, Fleet client credentials, and real [email provider configuration](company-accounts.md#email-delivery). Report storage uses its environment's private `REPORT_BUCKET` and task role. Neither broad provider secrets nor OpenAI credentials are transferred to workers. Both environment files leave email provider/sender unset until the operator supplies verified settings; deployment fails clearly rather than claiming mail is configured.

Build the reviewed image first. For the first company release, finish active runs/generation, stop every old web process and worker, run preflight and the backed-up explicit migration, configure mail, then deploy with `--company-cutover`. This requires the full administrative deployment so IAM grants the new company report prefix, retained legacy-read access, and optional SES sending permission. Old sessions and activation links expire; all migrated accounts verify their mapped email before login.

```text
uv run --locked python scripts/deploy_aws.py build --environment dev --profile mangorts-dev --revision REVIEWED-UNIQUE-REVISION
uv run --locked python scripts/deploy_aws.py status --environment dev --profile mangorts-dev
uv run --locked python scripts/deploy_aws.py deploy --environment dev --profile mangorts-dev --company-cutover
```

The cutover flag verifies all earlier-schema tasks/desktop sockets have stopped, then starts the company-aware image with `COMPANY_SCHEMA_VERSION=1`. Automatic rollback to an older account schema is disabled. A failed first cutover must be recovered with compatible code. Verify a healthy task/target, the intended digest, mail delivery and account/isolation behavior before restoring traffic and workers. Do not run these deployment or migration commands as part of local development verification.

Later releases use the ordinary release-only commands in [onboarding](developer-onboarding.md#deploy-the-shared-website). Local state files are not a distributed deployment lock; coordinate releases serially. Creating an approved new environment still uses `prepare`, `database`, then `database-ready` after initialization, which removes the temporary database-secret permission.

## Explicit production promotion

Production is disabled until its account/resource bindings, separate provider secret, domain access, initial database, email and per-user worker provisioning have been reviewed. Enabling the configuration is an operator decision; the script does not provision production in response to a development command. It refuses `build --environment prod`. Production uses the exact immutable image already built and reviewed in dev, so it does not rebuild different source during promotion.

After the dev deployment is healthy and the named revision has been reviewed, create a local review record:

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py promotion-manifest --environment dev --profile mangorts-dev --revision REVIEWED-UNIQUE-REVISION --reviewed-by REVIEWER --review-reference REVIEW-URL-OR-RECORD
```

This requires a stable current dev service and healthy targets. The manifest records account/region, exact source hash, revision, successful CodeBuild ID, immutable ECR image digest, dev task definition, reviewer, review reference and timestamp. The reviewer fields are an explicit operator attestation, not a cryptographic approval service. Existing review files are not overwritten. The command never deploys production.

Only after production has been explicitly enabled and prepared can an operator invoke:

```powershell
.venv/Scripts/python.exe scripts/deploy_aws.py deploy --environment prod --profile APPROVED-PROD-PROFILE --promotion-manifest .local/environments/dev/promotions/REVIEWED-UNIQUE-REVISION.json
```

Promotion rechecks manifest identity, successful source build and revision, registry digest, and that the referenced dev task definition ran that exact image. It supplies production database/provider secrets and roles, with `APP_ENV=prod` and `COMPANY_SCHEMA_VERSION=1`. It does not copy sessions, company/user records, passwords, worker credentials, assignments, run rows or reports from dev. The dev ECR artifact is shared read-only by digest; application state and report storage are separate. This path has been compiled and reviewed but has not been exercised against a production deployment.

## Kevin's development deployment access

GitHub collaborator `kevin-chengchihlee` has repository write access, as separately verified by the implementation pass. That does not establish an AWS IAM Identity Center identity or grant cloud access. No IAM role, SSO assignment, GitHub OIDC trust or CI workflow was created here.

An exact-username lookup in the account's existing Identity Center instance found neither `kevin` nor `kevin-chengchihlee`. The remaining input is Kevin's actual AWS identity, or the administrator-approved identity to create; a GitHub username is not silently turned into an AWS account.

The owner explicitly requested unrestricted provider access for Kevin. Assign his personal IAM Identity Center user the existing **AdministratorAccess** permission set for account `686458136219`, following the [AWS identity setup guide](developer-onboarding.md#aws-identities-and-full-account-access). The owner's existing `ScoobyXD` identity already has that permission set. The earlier proposal for a restricted release role is superseded; no restrictive permission set is required or being created.

The `build --release-only` and `deploy --release-only` commands remain useful with administrator credentials: they use existing resources and skip IAM policy changes, bucket configuration, CodeBuild project mutation and service creation. Environment/resource checks prevent accidentally selecting the wrong target regardless of the operator's provider permissions. The unrestricted operator paths perform infrastructure provisioning. Account-wide provider access is separate from each signed-in website member's assigned desktop.

A future GitHub `workflow_dispatch` path should use an exact repository/environment OIDC trust and explicit reviewed revision, with independent production approval/trust. It remains deferred; merging or pushing code currently deploys neither environment.
