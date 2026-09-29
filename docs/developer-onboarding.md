# Two developers, one development workspace

Windows and macOS use the same repository, shared development website and PostgreSQL database. Each person signs in separately and has a separate assigned Omarchy desktop. Workflows and history are shared inside **CTRL Loop Development**; a run always targets the signed-in member's own desktop. Company workspace checks apply to API reads, reports, downloads, worker delivery and desktop input.

The current development URL is <https://d1t0sm9jhdinfi.cloudfront.net>. The owner selected **`dev.ctrlloop.io`** under the existing domain; certificate/DNS activation and the app-origin cutover are pending renewal of the AWS sign-in session. Production is not deployed. See [environment and promotion configuration](environments.md).

## Repository setup

Kevin already has GitHub collaborator access as `kevin-chengchihlee`. Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) on the local computer, then use the same commands in PowerShell or a macOS terminal:

```text
git clone https://github.com/ScoobyXD/ctrlloop.git
cd ctrlloop
uv sync --locked --python 3.12
git status --short
```

For an existing checkout, preserve local changes before `git pull --ff-only`. Coordinate changes to shared files. This implementation was requested directly on `main`; ordinary contributor branches can be reviewed through the same repository without creating another website or database. Pushing code does not deploy either environment.

The pins remain Jev `jev-1.13.0`, TypeSafe SDK `0.7.1`, native cloud Driver `0.24.0`, Sandbox SDK `0.7.0`, and Fleet SDK `0.1.17`. Follow `AGENTS.md`: no new tests, no new Jev questions without the owner's audit, and preserve the separate Hermes launcher.

## Personal website access

After the identity migration, the original access password signs in as username **`owner`**. It is hashed as a personal credential during migration; it is not a fallback password for every member. Existing workflows, runs, events, reports, sessions and the assigned owner VM retain their identities.

An owner adds a member through **Workspace members**, using a username and display name. New members receive a single-use activation code with a 72-hour expiry; the app does not send email or contact anyone. The owner passes that code privately to the intended person. On the site's activation screen, inspect the named account/workspace, choose a personal password, and activate it. The code is then invalidated. Existing activated identities may join another workspace without sharing their password.

Kevin's intended username is **`kevin`**, display name **Kevin**. His activation receipt is created only when the approved migration/member setup runs. The receipt, if created through the admin command, is saved at `.local/environments/dev/workers/kevin/activation.json`; never commit or post it. A pending account cannot sign in or run workflows. Its assigned worker can be provisioned and report heartbeat status in advance.

Use the workspace selector if the identity belongs to more than one company. Every selection resets the displayed history and desktop connection. Members share workflow history inside their selected company, but cannot browse another company's records or select someone else's VM by changing an API parameter.

## Create and run a workflow

1. Open the shared development site and sign in with your personal username/password.
2. Check the displayed company workspace, your identity and your assigned desktop. A missing assignment is shown as pending provisioning.
3. Enter a request to save a workflow. The existing instruction model produces its saved instruction.
4. Return your desktop to **Agent** mode, then run the workflow. A busy, offline or maintenance desktop cannot start another run.
5. Inspect your VM in the embedded viewer and open the run's report or evidence ZIP. Creator and run initiator are recorded separately, so either member can run a shared workflow on their own desktop.

Takeover belongs to one signed-in session on one VM. It is disabled during an active run or deployment. Another developer can simultaneously use their own VM. Signing out, changing workspace, membership revocation and expired sessions invalidate control; the server rechecks input independently of the browser's view-only flag.

The executor still selects visible controls with its existing bounded loop. It does not add a planner, generated typing content or a general completion verifier. An `unverified` result is not success. Detailed questions, probabilities, timings, tokens and cost estimates remain in the report.

## Deploy only your worker

The administrator records a member assignment in the existing `workers` table and writes these local files:

```text
.local/environments/dev/workers/kevin/target.json
.local/environments/dev/workers/kevin/fleet/claim.json
```

These two identity files can accompany onboarding. They contain routing identifiers, not provider or worker credentials. `target.json` must name the actual sandbox, namespace, claim, member, workspace and development origin. The deploy tool compares them with the bound Fleet desktop and the server's worker identity before changing a service.

Independent worker deployment is pending Kevin's Fleet credential and a successful access check to the existing guest in `ctrlloop-omarchy-dev-kevin-20260928`. The owner has requested unrestricted provider access: leave the optional namespace selection empty, displayed as **All namespaces (no restriction)**, when creating the dedicated key. A namespace-limited key is no longer a prerequisite. See [Fleet credential handoff](#fleet-credential-handoff); the embedded website viewer needs no Fleet key on Kevin's computer.

After the Fleet access prerequisite is resolved and the administrator has configured the VM's dedicated worker/Jev credentials, updates reuse that guest configuration. Kevin does not need the shared database password, app bootstrap password, OpenAI key or another person's worker token:

```text
uv run --locked python scripts/deploy_worker.py --environment dev --member kevin --fleet-env .local/environments/dev/workers/kevin/fleet.env --status
uv run --locked python scripts/deploy_worker.py --environment dev --member kevin --fleet-env .local/environments/dev/workers/kevin/fleet.env --source-ref REVIEWED-COMMIT
```

Omit `--source-ref` only when deliberately deploying the current checkout, including local edits. The report records the resulting source-bundle revision. `--stage-only` uploads and compiles code without starting a worker or transferring credentials.

Deployment checks the exact assignment, takes the guest deployment lock, asks the server to put only that worker into maintenance, and refuses active/unsettled runs or human ownership. It starts the selected revision and releases maintenance after successful startup. Failed deployment keeps maintenance in place; inspect the privately recorded guest log before using the explicit `--resume` recovery operation. Never substitute another member's target or claim.

Initial setup uses `--configure --jev-env PATH`, plus that member's private `credentials.json`, under administrator control. That file contains the worker's dedicated bearer token. It is not part of a contributor onboarding bundle, source archive, or Git commit. Browser profiles are not copied between members.

When moving the existing owner worker to the selected custom domain, initial configuration also requires `--previous-api-url https://d1t0sm9jhdinfi.cloudfront.net`. That explicit transition still requires the exact existing worker identity and unchanged bearer token; the new origin must match the recorded assignment.

### Fleet credential handoff

Sign in as the account that owns the existing pools at [Fleet API keys](https://run.cua.ai/user-keys). Create a dedicated key named `ctrlloop-kevin`, leave **Allowed Namespaces (optional)** empty—the placeholder reads **All namespaces (no restriction)**—and save its one-time client ID/secret privately in ignored `.local/environments/dev/workers/kevin/fleet.env`. The official UI omits the scope restriction when no namespaces are selected. The key acts for its owner; a new personal Cua login does not automatically gain access to another account's pools. [Official Fleet credential setup](https://cua.ai/docs/how-to-guides/sandbox/set-up-fleet-credentials), [UI implementation](https://github.com/trycua/cua/blob/main/libs/fleet/src/pages/UserApiKeys.tsx).

The existing infrastructure credential returned **HTTP 403** from read-only `list_user_api_keys()`. Fleet's policy reserves key management for interactive clients, so selecting all namespaces does not let an API key create other keys. Use the signed-in key-management page. No new key has been created here. [Key-management policy](https://github.com/trycua/cua/blob/main/libs/fleet/backend/auth/authz_user_keys.rego).

To verify Kevin's credential, clear inherited `FLEETS_TOKEN`, configure `CUA_CLIENT_ID` and `CUA_CLIENT_SECRET`, and run the official guide's read-only token/namespace check. Then run the worker `--status` command above: successful namespace listing alone does not prove access to the already-assigned guest. Keep client secrets and bearer tokens out of output; the SDK key object's string representation includes its secret. Hand the dedicated credential to Kevin privately, separately from the non-secret target/claim files.

The official Fleet documentation reviewed does not establish an organization/member invitation procedure for sharing the existing pools with Kevin's separate dashboard identity. For that form of access, ask Cua to enable the appropriate account access; do not assume a new personal key shares the owner's pools. The credential guide links [Cua support](https://cua.ai/docs/how-to-guides/sandbox/set-up-fleet-credentials#create-a-fleet-user-api-key) for access problems. Full provider access is separate from CTRL Loop's per-member VM assignment, which remains enforced by the application.

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

These commands require the selected environment's database credential and network access. They operate on the existing schema and shared identity helpers, rather than a parallel account service:

```text
uv run --locked python scripts/member_admin.py --environment dev migrate-legacy-dev
uv run --locked python scripts/member_admin.py --environment dev add-member --workspace ctrlloop-development --member kevin --display-name Kevin
uv run --locked python scripts/member_admin.py --environment dev assign-worker --workspace ctrlloop-development --member kevin --claim .local/environments/dev/workers/kevin/fleet/claim.json --expected-sandbox ctrlloop-omarchy-dev-kevin-20260928-1683478c --fleet-env .env.fleet
uv run --locked python scripts/member_admin.py --environment dev list
```

Legacy migration saves a private row backup before the additive schema/data changes. Existing worker assignments cannot be silently replaced, and existing worker credentials cannot be silently rotated. The original VM's lifetime is unchanged. Kevin's new pool was created with a 48-hour creation-age TTL; the pool deadline is **2026-10-01 00:22:16 UTC** (September 30, 5:22:16 PM Pacific). Save important guest files before expiry. Durable uploaded reports outlive the VM, but unuploaded files and browser state do not. [Fleet expiry contract](https://cua.ai/docs/how-to-guides/sandbox/expire-pools-and-claims).
