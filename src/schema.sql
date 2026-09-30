-- Canonical company schema. Upgrade existing installations through company_migration.py.
CREATE TABLE IF NOT EXISTS companies (
    id uuid PRIMARY KEY, slug text NOT NULL UNIQUE, name text NOT NULL,
    join_password_hash text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    email text NOT NULL UNIQUE CHECK (email = lower(btrim(email))),
    display_name text NOT NULL, password_hash text,
    role text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'company_admin')),
    is_devadmin boolean NOT NULL DEFAULT false,
    email_verified_at timestamptz, disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(company_id, id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    user_id uuid NOT NULL, expires_at timestamptz NOT NULL,
    FOREIGN KEY(company_id, user_id) REFERENCES users(company_id, id)
);
CREATE TABLE IF NOT EXISTS workers (
    id text PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    assigned_user_id uuid NOT NULL, credential_hash text NOT NULL UNIQUE
        CHECK (credential_hash ~ '^[0-9a-f]{64}$'),
    fleet_namespace text NOT NULL CHECK (fleet_namespace ~ '^[a-z0-9][a-z0-9-]{0,199}$'),
    fleet_claim text NOT NULL CHECK (fleet_claim ~ '^[a-z0-9][a-z0-9-]{0,199}$'),
    mode text NOT NULL DEFAULT 'agent', human_session text,
    last_seen timestamptz, revision text, active_run uuid, disabled_at timestamptz,
    UNIQUE(company_id, id), UNIQUE(company_id, id, assigned_user_id),
    FOREIGN KEY(company_id, assigned_user_id) REFERENCES users(company_id, id)
);
CREATE TABLE IF NOT EXISTS loops (
    id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    created_by uuid NOT NULL, prompt text NOT NULL, instruction text, model text NOT NULL,
    status text NOT NULL DEFAULT 'generating', error text, llm_response_id text,
    llm_usage jsonb, created_at timestamptz NOT NULL DEFAULT now(), generated_at timestamptz,
    UNIQUE(company_id, id), FOREIGN KEY(company_id, created_by) REFERENCES users(company_id, id)
);
CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    loop_id uuid NOT NULL, worker_id text NOT NULL, initiated_by uuid NOT NULL,
    status text NOT NULL DEFAULT 'queued', created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz, finished_at timestamptz, error text, result jsonb, revision text,
    UNIQUE(company_id, id), UNIQUE(company_id, id, loop_id), UNIQUE(company_id, id, worker_id),
    FOREIGN KEY(company_id, loop_id) REFERENCES loops(company_id, id),
    FOREIGN KEY(company_id, initiated_by) REFERENCES users(company_id, id),
    FOREIGN KEY(company_id, worker_id, initiated_by) REFERENCES workers(company_id, id, assigned_user_id)
);
-- A worker cannot claim the active run of another worker or another company.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='workers'::regclass AND conname='workers_active_run_company_fk') THEN
        ALTER TABLE workers ADD CONSTRAINT workers_active_run_company_fk
            FOREIGN KEY(company_id, active_run, id) REFERENCES runs(company_id, id, worker_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    loop_id uuid NOT NULL, run_id uuid,
    stage text NOT NULL, message text NOT NULL, data jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(company_id, loop_id) REFERENCES loops(company_id, id),
    FOREIGN KEY(company_id, run_id, loop_id) REFERENCES runs(company_id, id, loop_id)
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash text PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), user_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
    expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(company_id, user_id) REFERENCES users(company_id, id)
);
-- Migration-only provenance, never used as an alternative membership model.
-- Records contain historical identifiers/roles, never passwords or token hashes.
CREATE TABLE IF NOT EXISTS company_migration_audit (
    id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id),
    record_type text NOT NULL, legacy_record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS sessions_company_user ON sessions(company_id, user_id);
-- Session bindings for Fleet's existing signed service credentials. Store only
-- the credential hash, never the signed URL. This is not another signing API.
-- Snapshot the assignment so moving a worker cannot redirect an open viewer.
CREATE TABLE IF NOT EXISTS desktop_connections (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    session_hash text NOT NULL REFERENCES sessions(token_hash) ON DELETE CASCADE,
    worker_id text NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    company_id uuid NOT NULL, assigned_user_id uuid NOT NULL,
    fleet_namespace text NOT NULL, fleet_claim text NOT NULL,
    admission_expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS desktop_connections_session ON desktop_connections(session_hash);
CREATE INDEX IF NOT EXISTS workers_company ON workers(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_worker_per_user ON workers(company_id, assigned_user_id) WHERE disabled_at IS NULL;
CREATE INDEX IF NOT EXISTS loops_company_time ON loops(company_id, created_at);
CREATE INDEX IF NOT EXISTS runs_company_time ON runs(company_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_reserved_run_per_worker ON runs(worker_id) WHERE status IN ('queued', 'running', 'paused');
CREATE INDEX IF NOT EXISTS events_company_loop_time ON events(company_id, loop_id, created_at);
CREATE INDEX IF NOT EXISTS auth_tokens_company_user ON auth_tokens(company_id, user_id, purpose);
CREATE INDEX IF NOT EXISTS auth_tokens_expiry ON auth_tokens(expires_at);
CREATE INDEX IF NOT EXISTS company_migration_audit_company ON company_migration_audit(company_id);

-- Authorization belongs to the real actor's session, including a devadmin's
-- home company. The run and its target relations remain scoped to company_id.
-- This soft reference permits logout/session deletion; dispatch revalidates it.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS request_session_hash text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS dispatch_before timestamptz;

-- Manual workbenches share the loop/run/event contract. A run owns an immutable
-- copy of its authored steps; editing a loop can never change dispatched input.
ALTER TABLE loops ADD COLUMN IF NOT EXISTS steps jsonb;
ALTER TABLE loops ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS steps jsonb;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS request_id uuid;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS next_step integer NOT NULL DEFAULT 0;
-- Execution bounds retain the full authored snapshot and its original indices.
-- A NULL end_step on an older run means the end of its saved steps.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS start_step integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS end_step integer;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS step_in_flight integer;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS step_started_at timestamptz;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS step_results jsonb NOT NULL DEFAULT '[]';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS run_mode text NOT NULL DEFAULT 'sequence';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS control text NOT NULL DEFAULT 'run';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS control_version integer NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS runs_request_id ON runs(company_id, initiated_by, request_id) WHERE request_id IS NOT NULL;
-- Create the stronger index before removing its predecessor, so startup never
-- leaves a gap in the desktop's single active reservation guarantee.
DROP INDEX IF EXISTS one_active_run_per_worker;

-- Existing authority rows invalidate live desktop grants across every backend
-- process. These are transient wakeups, not another session/ownership model.
-- Heartbeat timestamps and source revisions deliberately do not trigger them.
CREATE OR REPLACE FUNCTION ctrlloop_notify_desktop_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    previous_row jsonb;
    current_row jsonb;
    column_index integer;
    changed boolean := false;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        PERFORM pg_notify('ctrlloop_desktop_policy', '*');
        RETURN NULL;
    END IF;
    previous_row := to_jsonb(OLD);
    IF TG_OP = 'UPDATE' THEN
        current_row := to_jsonb(NEW);
        FOR column_index IN 1..TG_NARGS - 1 LOOP
            IF previous_row -> TG_ARGV[column_index] IS DISTINCT FROM current_row -> TG_ARGV[column_index] THEN
                changed := true;
                EXIT;
            END IF;
        END LOOP;
        IF NOT changed THEN RETURN NULL; END IF;
    END IF;
    PERFORM pg_notify('ctrlloop_desktop_policy', TG_ARGV[0] || ':' || (previous_row ->> TG_ARGV[1]));
    IF TG_OP = 'UPDATE' AND previous_row -> TG_ARGV[1] IS DISTINCT FROM current_row -> TG_ARGV[1] THEN
        PERFORM pg_notify('ctrlloop_desktop_policy', TG_ARGV[0] || ':' || (current_row ->> TG_ARGV[1]));
    END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER desktop_policy_sessions
AFTER DELETE OR UPDATE OF token_hash, company_id, user_id, expires_at ON sessions
FOR EACH ROW EXECUTE FUNCTION ctrlloop_notify_desktop_policy('session', 'token_hash', 'company_id', 'user_id', 'expires_at');
CREATE OR REPLACE TRIGGER desktop_policy_users
AFTER DELETE OR UPDATE OF id, company_id, email, password_hash, role, is_devadmin, email_verified_at, disabled_at ON users
FOR EACH ROW EXECUTE FUNCTION ctrlloop_notify_desktop_policy('user', 'id', 'company_id', 'email', 'password_hash', 'role', 'is_devadmin', 'email_verified_at', 'disabled_at');
CREATE OR REPLACE TRIGGER desktop_policy_workers
AFTER DELETE OR UPDATE OF id, company_id, assigned_user_id, credential_hash, fleet_namespace, fleet_claim, mode, human_session, active_run, disabled_at ON workers
FOR EACH ROW EXECUTE FUNCTION ctrlloop_notify_desktop_policy('worker', 'id', 'company_id', 'assigned_user_id', 'credential_hash', 'fleet_namespace', 'fleet_claim', 'mode', 'human_session', 'active_run', 'disabled_at');
CREATE OR REPLACE TRIGGER desktop_policy_connections
AFTER DELETE OR UPDATE OF token_hash, session_hash, worker_id, company_id, assigned_user_id, fleet_namespace, fleet_claim ON desktop_connections
FOR EACH ROW EXECUTE FUNCTION ctrlloop_notify_desktop_policy('connection', 'token_hash', 'session_hash', 'worker_id', 'company_id', 'assigned_user_id', 'fleet_namespace', 'fleet_claim');
CREATE OR REPLACE TRIGGER desktop_policy_companies
AFTER DELETE OR UPDATE OF id ON companies
FOR EACH ROW EXECUTE FUNCTION ctrlloop_notify_desktop_policy('company', 'id');

CREATE OR REPLACE TRIGGER desktop_policy_sessions_truncate AFTER TRUNCATE ON sessions
FOR EACH STATEMENT EXECUTE FUNCTION ctrlloop_notify_desktop_policy();
CREATE OR REPLACE TRIGGER desktop_policy_users_truncate AFTER TRUNCATE ON users
FOR EACH STATEMENT EXECUTE FUNCTION ctrlloop_notify_desktop_policy();
CREATE OR REPLACE TRIGGER desktop_policy_workers_truncate AFTER TRUNCATE ON workers
FOR EACH STATEMENT EXECUTE FUNCTION ctrlloop_notify_desktop_policy();
CREATE OR REPLACE TRIGGER desktop_policy_connections_truncate AFTER TRUNCATE ON desktop_connections
FOR EACH STATEMENT EXECUTE FUNCTION ctrlloop_notify_desktop_policy();
CREATE OR REPLACE TRIGGER desktop_policy_companies_truncate AFTER TRUNCATE ON companies
FOR EACH STATEMENT EXECUTE FUNCTION ctrlloop_notify_desktop_policy();
