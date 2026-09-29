# Two developers, one development workspace

Windows and macOS use the same repository, shared development website and PostgreSQL database. Each person signs in separately and has a separate assigned Omarchy desktop. Workflows and history are shared inside **CTRL Loop Development**; a run always targets the signed-in member's own desktop. Company workspace checks apply to API reads, reports, downloads, worker delivery and desktop input.

The current development URL is <https://d1t0sm9jhdinfi.cloudfront.net>. The requested `dev.controlloop.io` is pending domain confirmation/access; `ctrlloop.io` is a different existing domain. Production is not deployed. See [environment and promotion configuration](environments.md).

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

Independent worker deployment is pending a Fleet credential whose access is verified to be limited to Kevin's namespace, `ctrlloop-omarchy-dev-kevin-20260928`. No such key has been issued here; see [Fleet credential handoff](#fleet-credential-handoff) for the observed 403 and unresolved restriction. The embedded website viewer needs no Fleet key on Kevin's computer.

After the Fleet access prerequisite is resolved and the administrator has configured the VM's dedicated worker/Jev credentials, updates reuse that guest configuration. Kevin does not need the shared database password, app bootstrap password, OpenAI key or another person's worker token:

```text
uv run --locked python scripts/deploy_worker.py --environment dev --member kevin --fleet-env .local/environments/dev/workers/kevin/fleet.env --status
uv run --locked python scripts/deploy_worker.py --environment dev --member kevin --fleet-env .local/environments/dev/workers/kevin/fleet.env --source-ref REVIEWED-COMMIT
```

Omit `--source-ref` only when deliberately deploying the current checkout, including local edits. The report records the resulting source-bundle revision. `--stage-only` uploads and compiles code without starting a worker or transferring credentials.

Deployment checks the exact assignment, takes the guest deployment lock, asks the server to put only that worker into maintenance, and refuses active/unsettled runs or human ownership. It starts the selected revision and releases maintenance after successful startup. Failed deployment keeps maintenance in place; inspect the privately recorded guest log before using the explicit `--resume` recovery operation. Never substitute another member's target or claim.

Initial setup uses `--configure --jev-env PATH`, plus that member's private `credentials.json`, under administrator control. That file contains the worker's dedicated bearer token. It is not part of a contributor onboarding bundle, source archive, or Git commit. Browser profiles are not copied between members.

### Fleet credential handoff

Fleet documents an **Allowed Namespaces** selector. Its official UI passes namespace names directly in `scope`, and the installed SDK accepts `CreateUserApiKeyRequest(name="ctrlloop-kevin-dev", scope=["ctrlloop-omarchy-dev-kevin-20260928"])`. This is the verified request format, not evidence that the resulting credential is restricted. [Fleet credential guide](https://cua.ai/docs/how-to-guides/sandbox/set-up-fleet-credentials), [official UI](https://github.com/trycua/cua/blob/main/libs/fleet/src/pages/UserApiKeys.tsx).

The existing infrastructure credential returned **HTTP 403** from the read-only `list_user_api_keys()` call. Official policy permits key management only through interactive clients (`cyclops-cs-spa`, `cua-cli`, `cua-desktop`); programmatic user keys cannot mint, list or revoke other keys. No creation was attempted. An authorized account's interactive key-management session is required; do not broaden or distribute the existing infrastructure credential to work around the denial. [Key-management policy](https://github.com/trycua/cua/blob/main/libs/fleet/backend/auth/authz_user_keys.rego).

Namespace enforcement remains **unverified**. The reviewed public implementation stores `scope` as a Keycloak client attribute, but the inspected user-key claim mapping and authorization paths do not establish enforcement of that list. Public source may differ from the running service. Do not treat a successful creation response or displayed namespace label as proof of isolation; obtain provider confirmation and verify the effective restriction before issuing a contributor credential. [Key creation](https://github.com/trycua/cua/blob/main/libs/fleet/backend/keycloak/admin.go), [token handling](https://github.com/trycua/cua/blob/main/libs/fleet/backend/auth/middlewares.go), [namespace policy](https://github.com/trycua/cua/blob/main/libs/fleet/backend/auth/authz_namespaces.rego).

Until that is resolved, an authorized administrator can deploy Kevin's reviewed source using the exact member target while keeping infrastructure credentials private. After restricted access is confirmed, hand the dedicated client ID/secret only to Kevin through a private channel and save it in ignored `.local/environments/dev/workers/kevin/fleet.env` with access limited to its owner. Do not print the SDK's returned key object: its string representation includes the secret. Clear any inherited `FLEETS_TOKEN` before using the dedicated OAuth credential, because that variable takes precedence. The non-secret target/claim files can accompany onboarding separately; provider keys, activation codes and guest worker/Jev credentials never belong in a shared source bundle.

## Deploy the shared website

A web/backend deployment changes the site for both developers. Coordinate one release at a time and wait for instruction generation and runs to be idle. Use an approved AWS SSO development release identity and the non-secret development resource state; do not distribute `.local/environments/dev/deployment-secrets.json`.

```text
uv run --locked python scripts/deploy_aws.py build --environment dev --profile YOUR-DEV-SSO-PROFILE --revision UNIQUE-REVIEWED-REVISION --release-only
uv run --locked python scripts/deploy_aws.py status --environment dev --profile YOUR-DEV-SSO-PROFILE
uv run --locked python scripts/deploy_aws.py deploy --environment dev --profile YOUR-DEV-SSO-PROFILE --release-only
uv run --locked python scripts/deploy_aws.py status --environment dev --profile YOUR-DEV-SSO-PROFILE
```

`--release-only` uses the existing build project, roles, buckets and service. It does not create IAM roles, rewrite permission policies, reconfigure the build project, create buckets or create another service. Account/resource guards still run. Wait for the requested image's healthy, completed rollout; a successful build alone is not a deployment. Local state is not a distributed deployment lock, so the two operators must coordinate releases.

Kevin's AWS Identity Center username/identity has not been supplied or assigned by this work. The exact permitted resource boundaries are documented in [environments.md](environments.md#kevins-development-deployment-access); do not grant administrator access to make this command work. Production uses a separate explicit reviewed-image promotion path, separate secrets and data, and remains disabled until its configuration is approved.

## Administrator migration and assignment

These commands require the selected environment's database credential and network access. They operate on the existing schema and shared identity helpers, rather than a parallel account service:

```text
uv run --locked python scripts/member_admin.py --environment dev migrate-legacy-dev
uv run --locked python scripts/member_admin.py --environment dev add-member --workspace ctrlloop-development --member kevin --display-name Kevin
uv run --locked python scripts/member_admin.py --environment dev assign-worker --workspace ctrlloop-development --member kevin --claim .local/environments/dev/workers/kevin/fleet/claim.json --expected-sandbox ctrlloop-omarchy-dev-kevin-20260928-1683478c --fleet-env .env.fleet
uv run --locked python scripts/member_admin.py --environment dev list
```

Legacy migration saves a private row backup before the additive schema/data changes. Existing worker assignments cannot be silently replaced, and existing worker credentials cannot be silently rotated. The original VM's lifetime is unchanged. Kevin's new pool was created with a 48-hour creation-age TTL; the pool deadline is **2026-10-01 00:22:16 UTC** (September 30, 5:22:16 PM Pacific). Save important guest files before expiry. Durable uploaded reports outlive the VM, but unuploaded files and browser state do not. [Fleet expiry contract](https://cua.ai/docs/how-to-guides/sandbox/expire-pools-and-claims).
