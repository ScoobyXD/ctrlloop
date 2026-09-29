# Developer accounts and assigned desktops

Windows and macOS use the same repository, shared development website and PostgreSQL database. Each person signs in separately and has a separate assigned Omarchy desktop. Workflows and history are shared inside **CTRL Loop Development**; a run always targets the signed-in member's own desktop. Company checks apply to API reads, reports, downloads, worker delivery and desktop input.

The configured development origin is **`https://dev.ctrlloop.io`**. Company accounts are implemented in source, and require the explicit migration/email setup/release in [company accounts](company-accounts.md). This change did not deploy or migrate the live environment. Production remains separately configured. See [environment and promotion configuration](environments.md).

## Repository setup

Kevin already has GitHub collaborator access as `kevin-chengchihlee`. Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) on the local computer, then use the same commands in PowerShell or a macOS terminal:

```text
git clone https://github.com/ScoobyXD/ctrlloop.git
cd ctrlloop
uv sync --locked --python 3.12
git status --short
```

For an existing checkout, preserve local changes before `git pull --ff-only`. Coordinate changes to shared files. Company account implementation is isolated in its own `codex/` branch and checkout; review changes before merging or releasing. Pushing code does not deploy either environment.

The pins remain Jev `jev-1.13.0`, TypeSafe SDK `0.7.1`, native cloud Driver `0.24.0`, Sandbox SDK `0.7.0`, and Fleet SDK `0.1.17`. Follow `AGENTS.md`: no new tests, no new Jev questions without the owner's audit, and preserve the separate Hermes launcher.

## Personal website access

Use normal signup with your email and one personal password. **Company admin** creates a new company and a separate company enrollment password. **User** joins an existing company with its company password. Verify your email before login. There are no manual activation codes or second personal-password entry. Forgotten-password links reset only your personal password.

Each person belongs to exactly one company. A company admin manages its users and join password; ordinary users cannot select another company. VM assignment remains per user. An account can use saved loops before a VM is assigned, with run/desktop controls showing that assignment is pending.

Platform devadmin is a separate explicit operator grant after verified signup. Jonathan provided `jonathankim829@gmail.com`; Kevin's email is still needed. No privilege is derived from display names or self-submitted roles. See [the grant commands and trusted target context](company-accounts.md#platform-devadmin).

## Create and run a workflow

1. Open the shared development site and sign in with your personal email/password.
2. Check the displayed company, your identity and your assigned desktop. A missing assignment is shown as pending provisioning.
3. Enter a request to save a workflow. The existing instruction model produces its saved instruction.
4. Return your desktop to **Agent** mode, then run the workflow. A busy, offline or maintenance desktop cannot start another run.
5. Inspect your VM in the embedded viewer and open the run's report or evidence ZIP. Creator and run initiator are recorded separately, so either member can run a shared workflow on their own desktop.

Takeover belongs to one signed-in session on one VM. It is disabled during an active run or deployment. Another developer can simultaneously use their own VM. Signing out, access revocation and expired sessions invalidate control; the server rechecks input independently of the browser's view-only flag.

The executor still selects visible controls with its existing bounded loop. It does not add a planner, generated typing content or a general completion verifier. An `unverified` result is not success. Detailed questions, probabilities, timings, tokens and cost estimates remain in the report.

## Deploy only your worker

The administrator records the exact company/user assignment in `workers`. The assignment command prints a local directory under `.local/environments/dev/workers/<sha256-of-normalized-email>/`, containing `target.json` and `fleet/claim.json`. These identity files contain routing identifiers, not provider or worker credentials. A separate private `credentials.json` contains the per-worker bearer token.

`target.json` records the exact sandbox, namespace, claim, normalized email, `company_id`, `assigned_user_id`, and development origin. Deployment compares them with the bound Fleet desktop and bearer-authenticated server assignment before changing a service. Existing username-based files are retained; use the [explicit migration/assignment procedure](company-accounts.md#per-user-vm-assignment) to create the new receipt without rotating a token or moving a VM.

Both developers use the **same existing Jev key, OpenAI key and Fleet client ID/secret**, retrieved through their own AWS SSO identities. Follow [Shared provider credentials](#shared-provider-credentials) once your AWS account assignment is ready. The embedded website viewer needs no Fleet key on your computer.

After the administrator has configured the VM's dedicated worker/Jev credentials, updates reuse that guest configuration. The provider file also supports local development; worker updates only consume its Fleet credentials:

```text
uv run --locked python scripts/deploy_worker.py --environment dev --email USER-EMAIL --fleet-env .local/environments/dev/shared.env --status
uv run --locked python scripts/deploy_worker.py --environment dev --email USER-EMAIL --fleet-env .local/environments/dev/shared.env --source-ref REVIEWED-COMMIT
```

Omit `--source-ref` only when deliberately deploying the current checkout, including local edits. The report records the resulting source-bundle revision. `--stage-only` uploads and compiles code without starting a worker or transferring credentials.

Deployment checks the exact assignment, takes the guest deployment lock, asks the server to put only that worker into maintenance, and refuses active/unsettled runs or human ownership. It starts the selected revision and releases maintenance after successful startup. Failed deployment keeps maintenance in place; inspect the privately recorded guest log before using the explicit `--resume` recovery operation. Never substitute another user's target or claim.

Initial setup uses `--configure --jev-env .local/environments/dev/shared.env`, plus that member's private `credentials.json`, under administrator control. That file contains the worker's dedicated bearer token. It is not part of a contributor onboarding bundle, source archive, or Git commit. Browser profiles are not copied between members.

When moving the existing owner worker to the selected custom domain, initial configuration also requires `--previous-api-url https://d1t0sm9jhdinfi.cloudfront.net`. That explicit transition still requires the exact existing worker identity and unchanged bearer token; the new origin must match the recorded assignment.

## Shared provider credentials

AWS Secrets Manager is the shared source of the existing credentials. `ctrlloop/mvp/app` holds `TYPESAFE_API_KEY`, `CUA_CLIENT_ID` and `CUA_CLIENT_SECRET`; OpenAI remains in the separately configured `mangorts/dev/openai-api-key` secret. The app's existing secret references are reused. This setup creates no new provider keys, changes no provider permissions and does not rotate credentials.

After configuring your personal [AWS SSO identity](#aws-identities-and-full-account-access), run this from the repository on macOS or Windows:

```text
aws sso login --profile mangorts-dev
uv run --locked python scripts/provider_credentials.py fetch --environment dev --profile mangorts-dev
```

The retrieval command verifies the AWS account, writes **only** `TYPESAFE_API_KEY`, `OPENAI_API_KEY`, `CUA_CLIENT_ID` and `CUA_CLIENT_SECRET` to `.local/environments/dev/shared.env`, and verifies the file matches the retrieved values. It prints variable names and version metadata, never values. The file is ignored and untracked, replaced atomically, and created with owner-only permissions (`0600`) on macOS/Linux. Rerun the same command to refresh your local copy. It does not export the app/database credentials or any VM's `WORKER_TOKEN`.

Pass that file to the existing `--fleet-env` and `--jev-env` arguments. To use its values for local commands in a macOS terminal:

```sh
set -a
. .local/environments/dev/shared.env
set +a
unset FLEETS_TOKEN
```

The file quotes values so sourcing it cannot expand shell substitutions. `FLEETS_TOKEN` otherwise takes precedence over Fleet client credentials. Clear previously inherited `CUA_CLIENT_ID`/`CUA_CLIENT_SECRET` before using `--fleet-env`, or source the retrieved file as shown, because the worker tool preserves existing process environment variables. Keep this file out of frontend bundles, screenshots and logs. [TypeSafe authentication](https://docs.typesafe.ai/api), [OpenAI secret/environment guidance](https://developers.openai.com/api/docs/guides/production-best-practices#api-keys), [Fleet credential configuration](https://cua.ai/docs/how-to-guides/sandbox/set-up-fleet-credentials#configure-the-sandbox-sdk).

Fleet access uses the same existing credential used for the project pools. This setup adds no namespace restriction and does not create or rotate a separate Kevin key. Its existing provider permissions remain unchanged; access to a particular guest must still be verified. Sharing it does not create a separate Cua dashboard login. CTRL Loop's personal VM assignments and the dedicated per-VM worker bearer tokens remain separate from shared provider access.

The owner's one-time import of the existing local Jev key uses:

```text
uv run --locked python scripts/provider_credentials.py seed-jev --environment dev --profile mangorts-dev --jev-env .env
```

This reads the current project secret, adds only the exact existing Jev value, and preserves every unrelated field. A guarded version-stage move refuses a concurrent secret change. Repeating it with the same key is a no-op; a different stored Jev key is rejected. Kevin only needs `fetch`. No model request, VM configuration or website deployment is performed by either command.

## AWS identities and full account access

Read-only verification on **September 29, 2026 UTC** confirmed that the owner already has IAM Identity Center username **`ScoobyXD`**, display name **Jonathan Kim**, in account **`686458136219`**. The existing `mangorts-dev` profile uses the **`AdministratorAccess`** permission set in **`us-west-1`**. STS reported the corresponding `AWSReservedSSO_AdministratorAccess_9dc9077959c6397f/ScoobyXD` role session; the role has AWS's `AdministratorAccess` managed policy, no permissions boundary and no inline policies. There is no need to create another identity for the owner.

Sign in through the [AWS access portal](https://d-916771bd28.awsapps.com/start). To give Kevin the full account access requested:

1. Open [IAM Identity Center in us-west-1](https://us-west-1.console.aws.amazon.com/singlesignon/home?region=us-west-1), choose **Users → Add user**, and enter Kevin's chosen username, email, first/last name and display name. Choose email setup instructions or a one-time password to deliver privately. [AWS user-creation guide](https://docs.aws.amazon.com/singlesignon/latest/userguide/addusers.html).
2. Under **AWS accounts**, select account **686458136219**, choose **Assign users or groups**, select Kevin and the existing **AdministratorAccess** permission set, then submit and wait for completion. No custom restrictive permission set is needed for this request. [AWS assignment guide](https://docs.aws.amazon.com/singlesignon/latest/userguide/assignusers.html), [AdministratorAccess policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AdministratorAccess.html).
3. Kevin signs into the same portal using his own identity. On his computer, configure and verify the CLI profile below, selecting this account and `AdministratorAccess`. [AWS CLI SSO setup](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

```text
aws configure sso --profile mangorts-dev
aws sso login --profile mangorts-dev
aws sts get-caller-identity --profile mangorts-dev
```

The wizard uses start URL `https://d-916771bd28.awsapps.com/start`, SSO region `us-west-1`, registration scope `sso:account:access`, account `686458136219`, and role `AdministratorAccess`. Kevin's identity details and preferred password/invitation delivery are still needed before creating his account. This verification created no users, assignments, invitations or keys. The metadata-only evidence is saved privately at `.local/mvp/aws-self-identity-20260929.json`.

## Deploy the shared website

A web/backend deployment changes the site for both developers. Coordinate one release at a time and wait for instruction generation and runs to be idle. Use an approved AWS SSO development release identity and the non-secret development resource state; do not distribute `.local/environments/dev/deployment-secrets.json`.

```text
uv run --locked python scripts/deploy_aws.py build --environment dev --profile YOUR-DEV-SSO-PROFILE --revision UNIQUE-REVIEWED-REVISION --release-only
uv run --locked python scripts/deploy_aws.py status --environment dev --profile YOUR-DEV-SSO-PROFILE
uv run --locked python scripts/deploy_aws.py deploy --environment dev --profile YOUR-DEV-SSO-PROFILE --release-only
uv run --locked python scripts/deploy_aws.py status --environment dev --profile YOUR-DEV-SSO-PROFILE
```

`--release-only` uses the existing build project, roles, buckets and service. It does not create IAM roles, rewrite permission policies, reconfigure the build project, create buckets or create another service. Account/resource guards still run. Wait for the requested image's healthy, completed rollout; a successful build alone is not a deployment. Local state is not a distributed deployment lock, so the two operators must coordinate releases.

Kevin's AWS identity has not yet been supplied or assigned by this work. Follow [AWS identities and full account access](#aws-identities-and-full-account-access) for the requested `AdministratorAccess` assignment. The deployment tool still checks the selected environment and exact resources, even when its operator has full provider privileges. Production uses a separate explicit reviewed-image promotion path, separate secrets and data, and remains disabled until its configuration is approved.

## Administrator migration and assignment

Company migration and verified-account administration use the existing environment database credential. Follow [preflight, explicit ownership mappings, backup, maintenance cutover, activation, and assignment](company-accounts.md#migration-preflight). Preflight is read-only; do not run the migration while old web processes or workers remain live. No account migration, VM provisioning, live deployment, or email was performed by the source change.

```text
uv run --locked python scripts/member_admin.py --environment dev preflight-companies
uv run --locked python scripts/member_admin.py --environment dev list
uv run --locked python scripts/member_admin.py --environment dev set-devadmin --email jonathankim829@gmail.com --grant
```

The grant requires Jonathan's enabled, verified account and revokes its sessions. VM assignment requires an explicit company, user email, claim, and expected sandbox; it refuses conflicting assignments and never silently rotates an existing credential. Refer to the preserved [VM lifetime readback](vm-lifetime-20260927.md) for historical evidence; current provider lifetime must be verified before relying on a guest. Durable uploaded reports outlive the VM, while unuploaded files and browser state do not.
