# Company accounts and safe migration

This source change implements one company per account. The configured development origin is `https://dev.ctrlloop.io`; verify deployment state independently before a release.

## Accounts and ownership

`companies` is the tenant parent. Every company-owned table has an indexed, non-null `company_id` foreign key: `users`, `sessions`, `workers`, `loops`, `runs`, `events`, `auth_tokens`, and the historical `company_migration_audit`. Composite foreign keys bind children, creators, initiators, assignments, tokens, and session owners to the same company. `users.company_id` establishes exactly one home company; there is no parallel membership table or company-switching API for ordinary users.

Signup uses an email address and one personal password. **Company admin** creates a new company and chooses its separate company password. **User** selects an existing company and supplies its company password. The selector returns only company IDs and names. Company passwords use separate salted scrypt hashes and authorize initial enrollment only. Personal credentials handle subsequent login; changing the company password does not modify existing accounts. Company admins can rename their company, change its join password, and manage company users.

Email verification is required before login. Verification and reset links expire and are single use, and only token hashes are stored. Resetting the personal password revokes existing sessions. Logout invalidates the session; cookies remain Secure, HttpOnly, and SameSite. Mutations require the configured same origin. Signup and account endpoints use rate limits, and signup/password/role/company assignment runs atomically.

An ordinary account can read company loops/reports and use its own assigned desktop. The backend checks company ownership for object IDs and credentials; changing a company/user parameter cannot expand ordinary access. Worker tokens resolve one database assignment and cannot poll or submit evidence for another user's run. No signup action provisions a paid VM. A user without an assigned VM can still create and inspect loops, and sees an explicit unavailable-desktop state.

At startup, the guest validates the existing heartbeat response against its configured `CTRLLOOP_WORKER_ID` and `CTRLLOOP_ENVIRONMENT`. Each delivered run must match that VM, company, and assigned user; the guest refreshes the handshake before creating its report/journal or sending native input. New journals retain the run's company, worker and initiator. A mismatched assignment fails closed. Older journals without those fields may only recover evidence/results through the credential-authorized server endpoints and are never executed again. Existing dedicated guest configuration needs no new company environment variables.

New private report objects use `companies/<company_id>/runs/<run_id>/worker.zip`. An authorized run lookup can still read its original `runs/<run_id>/worker.zip` if the new key is absent. No report objects are moved or deleted by migration. Artifact metadata remains in the company-owned run row.

## Platform devadmin

Devadmin is a separate server-controlled `users.is_devadmin` privilege. It is never a signup choice or company role, and signing up with a familiar name or email does not grant it. A devadmin has a normal account and home company and completes ordinary signup and verification first. An operator then grants the privilege explicitly:

```text
uv run --locked python scripts/member_admin.py --environment dev set-devadmin --email jonathankim829@gmail.com --grant
uv run --locked python scripts/member_admin.py --environment dev set-devadmin --email jonathankim829@gmail.com --revoke
```

Both commands revoke that account's sessions, requiring a fresh login. Granting requires an enabled, verified account. Jonathan supplied the address above; Kevin's email has not been supplied, so no Kevin privilege is configured. Provider/AWS administrator access is separate from website devadmin.

Devadmins can list companies and explicitly choose a target company and user. HTTP requests carry `X-Company-ID` and `X-User-ID`; desktop WebSockets use `company_id` and `user_id` query parameters. The server validates the trusted devadmin privilege and that the target user belongs to that company. A target changes the operating context, never the devadmin's home membership or anyone's VM assignment. Ordinary requests remain tied to the account's home company and own desktop.

## Email delivery

The application supports real SMTP and AWS SES. Development uses AWS SES in us-west-1 with the DNS-verified sender domain `dev.ctrlloop.io` and sender `no-reply@dev.ctrlloop.io`. Production remains unconfigured. Verify SES account sending eligibility before opening signup to arbitrary recipients; a sandbox account accepts only verified recipients. Without configuration, account responses explicitly report email delivery unavailable. They do not claim a message was sent. Deployments refuse to proceed with missing real email configuration.

| Setting | Meaning |
| --- | --- |
| `EMAIL_PROVIDER` | `smtp`, `ses`, or local development only `local` |
| `EMAIL_FROM` | Verified sender for real mail |
| `SMTP_HOST`, `SMTP_PORT` | SMTP host and port; STARTTLS normally uses 587, TLS normally 465 |
| `SMTP_SECURITY` | `starttls` or `ssl`; transport encryption is required |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Optional provider authentication; store in the application secret |
| `AWS_REGION` | SES region, matching the verified sender and task permissions |
| `EMAIL_LOCAL_DIR` | Development capture directory, default `.local/mail`; used only with `APP_ENV=dev` and `EMAIL_PROVIDER=local` |

Local mode writes `.eml` files and the UI says mail was captured locally. These files contain token links and must remain private. Do not enable local mode on a deployed site. For SES, verify the sender and account sending access in the selected region; an SES sandbox may require verified recipients. For SMTP, obtain the sender and provider's authentication settings. No email credentials are placed in frontend files, reports, or logs.

Provider references: [AWS SES sending requirements](https://docs.aws.amazon.com/boto3/latest/reference/services/ses/client/send_email.html) and [Python SMTP TLS support](https://docs.python.org/3/library/smtplib.html).

The current deployment runs one web process. Peer, email, and company enrollment attempt limits are bounded in that process; token issuance also has a persistent per-account limit. Before horizontally scaling the web service, move attempt counters into shared infrastructure so replicas cannot multiply the limits. Company administration writes use a shared PostgreSQL advisory lock; session/desktop operations use consistent user, session, and worker lock ordering.

For the deployment script, configure the selected `config/environments/<environment>.json` `email` object with `provider` and `from`. SMTP also accepts `smtp_host`, `smtp_port`, `smtp_security`, and `smtp_auth` (default true). Store `SMTP_USERNAME` and `SMTP_PASSWORD` in that environment's existing application secret when authentication is enabled. SES uses the application's AWS task role. A full administrative deployment installs the sender-constrained SES policy and report-prefix policy. The first company cutover must use this full deployment, since `--release-only` intentionally does not update IAM policies.

## Migration preflight

Never reset the database. Use the selected environment's existing private database credential and network access. These commands do not create another database:

```text
uv run --locked python scripts/member_admin.py --environment dev preflight-companies
uv run --locked python scripts/member_admin.py --environment dev preflight-companies --mapping .local/company-migration.json
```

Preflight reports state, row counts, actionable record IDs, and the exact mapping format; it never prints credential hashes. Existing usernames require an explicit real email mapping. A user with more than one historical membership requires an explicit home-company choice, even if an old membership is revoked. Resolve cross-company historical creator/worker/run relationships explicitly; migration refuses an inconsistent or ambiguous assignment rather than silently moving data or deleting history. A selected revoked membership remains disabled. Every company must retain an enabled company admin who can verify the mapped email and set its enrollment password; preflight refuses companies without one. Old authorization memberships are archived as company-scoped migration records, not retained as an active access model.

The mapping file supports these sections. Omit unused sections and replace placeholders with actual IDs from preflight:

```json
{
  "users": {
    "LEGACY_USER_UUID": {"email": "person@example.com", "company_id": "COMPANY_UUID"}
  },
  "loops": {
    "LOOP_UUID": {"company_id": "COMPANY_UUID", "created_by": "USER_UUID"}
  },
  "workers": {
    "EXACT_WORKER_ID": {"company_id": "COMPANY_UUID", "assigned_user_id": "USER_UUID"}
  },
  "runs": {
    "RUN_UUID": {"initiated_by": "USER_UUID"}
  }
}
```

For the older pre-identity schema, preflight also describes explicit `create_companies`, `create_users`, and `unowned_sessions` mappings. Missing worker credentials can be supplied only through an explicitly named environment variable using `credential_token_env`, with exact Fleet namespace/claim mappings. Optional initial personal credentials for newly mapped legacy users use `password_env`. Never put plaintext secrets into mapping files or the command line. Existing hashes, row IDs, company IDs, worker identities, and available evidence are preserved.

## Cutover and activation

Build/review the new image first. Schedule a maintenance window, finish queued/running runs and instruction generation, stop all old web processes (including desktop WebSockets) and worker services, and verify they are stopped. Use the existing ECS/service administration controls; neither ordinary startup nor preflight performs a live migration.

```text
uv run --locked python scripts/member_admin.py --environment dev migrate-companies --mapping .local/company-migration.json --application-stopped
```

The explicit command takes a private row backup under `.local/environments/dev/backups/`, locks the legacy tables, reruns validation, and migrates in one transaction. Failure rolls back DB changes. Backups include credentials/hashes and must be protected as private database backups. The routine is repeatable against the resulting company schema. Runtime startup creates an empty company schema or validates a current schema, but refuses legacy migration without the explicit opt-in used by this CLI.

Deploy the reviewed image with `scripts/deploy_aws.py deploy --environment dev --company-cutover` and the normal environment/profile arguments. This flag drains all earlier-schema tasks before starting company-aware code, stamps `COMPANY_SCHEMA_VERSION=1`, and disables automatic rollback to an incompatible image. Do not restore an old image against the migrated schema. The first cutover requires a full administrative deployment to update company report storage and optional SES permissions; later releases can use `--release-only`. Verify health, signup, mail delivery, tenant boundaries, and worker assignments before restoring normal use.

Migration expires old sessions and invalidates activation links. Mapped accounts must verify their email. Existing personal password hashes remain valid; accounts without a personal password use the verified email/password-reset flow. Legacy companies initially have no join-password hash, so new enrollment is disabled until a company admin sets one. Devadmin grants happen only after the account is verified.

## Per-user VM assignment

Provision an approved VM separately with the existing integration. Assignment never creates, renews, or charges for a VM. After signup/verification, explicitly bind its company, user email, and exact Fleet claim:

```text
uv run --locked python scripts/member_admin.py --environment dev assign-worker --company COMPANY-SLUG --email USER-EMAIL --claim PATH-TO-CLAIM.json --expected-sandbox EXACT-SANDBOX --fleet-env .env.fleet
uv run --locked python scripts/member_admin.py --environment dev list
uv run --locked python scripts/deploy_worker.py --environment dev --email USER-EMAIL --fleet-env .env.fleet --status
```

The assignment command refuses a different existing company/user/claim or a second active VM. New tokens are generated only for new worker rows. An existing worker requires its existing credential; use `--existing-credentials PATH` to import its dedicated credential receipt explicitly without rotating the token. For a pre-identity worker, first preserve the old bearer token in a private receipt containing `environment`, `worker_id`, and `WORKER_TOKEN`; do not post or print it.

Files now live under `.local/environments/<environment>/workers/<sha256-of-normalized-email>/`. The command prints the actual directory. Re-running assignment for a migrated user writes new `target.json` and `credentials.json` containing `company_id` and `assigned_user_id`; old username directories are left intact. `target.json` and the claim contain identifiers; `credentials.json` is private. Deployment compares environment, company, user, sandbox, and bearer-authenticated server identity before taking that worker into maintenance. Initial `--configure --jev-env PATH` transfers only that worker's dedicated token and the approved Jev key. No default/global worker is selected by the application or deployment commands.

## Local verification of this implementation

Verification used an isolated PostgreSQL 18 cluster, synthetic accounts, and inline smoke commands. No test files or suites were added. No live database, cloud VM, mail provider, or deployment was changed.

| Area | Result |
| --- | --- |
| Schema and migration | Fresh initialization and repeat startup passed. Legacy and pre-identity fixtures migrated with explicit mappings; IDs, password hashes, and records were preserved. Ambiguous membership/email mappings were refused. An injected dependency failure rolled back the migration. |
| Database isolation | PostgreSQL rejected eight invalid relationship cases across sessions, users, workers, loops, runs, events, and tokens, including wrong-loop events and another worker's active run. Missing composite constraints were detected by preflight. |
| Accounts | Company creation/join, wrong/right company password, role-escalation rejection, verification gating, single-use/expired tokens, password reset, cookie invalidation, logout, origin checks, and attempt limiting passed. Rejected request validation did not echo credentials. |
| Company administration | Join-password rotation preserved existing personal login. Devadmin target selection accepted valid company/user pairs and refused mismatches. Concurrent cross-company administration and session cleanup checks passed after lock-order fixes. |
| Worker startup | With local HTTP/OS/executor substitutes, copied credentials, foreign deliveries, and foreign journals were refused before native execution. A valid delivery wrote the matching company/user/worker fields before execution. A pending, unverified target was refused at run creation. |
| Product isolation | Loop/event ownership, scoped background generation, no-VM behavior, per-user run dispatch, worker credential checks, deployment identity checks, and cross-company report/download/file refusal passed. Model generation and private object storage used local substitutes. |
| Desktop authorization | Own-worker targeting, cross-company socket refusal, input mode changes, explicit devadmin VM selection, live privilege revocation, and control release on logout passed with a substitute gateway. Real Fleet/VNC connectivity was not exercised. |
| Browser UI | Admin signup, local verification, login/logout, company settings display, no-VM state, wrong/right company enrollment, password visibility, recovery request, and reset-link form handling passed. Automatic approval review blocked company-password submission, a second fixture's verification, and repeat sign-in; those browser steps and final reset submission remain unverified. Independent API checks exercised the account behavior. |
| Static and operations | Python compilation, JavaScript syntax, DOM IDs/labels, management/deployment CLI help, local environment configuration, artifact-key handling, and diff whitespace checks passed. |

Mail was captured locally and reported as such. SMTP/SES delivery, deployed IAM permissions, live database mappings, and actual desktop connectivity remain operator integration checks after configuration and an authorized cutover. Jonathan's intended devadmin email is documented above; no live privilege has been granted.
