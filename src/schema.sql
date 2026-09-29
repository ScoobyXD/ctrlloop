CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY, expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    id text PRIMARY KEY, mode text NOT NULL DEFAULT 'agent', human_session text,
    last_seen timestamptz, revision text, active_run uuid
);
CREATE TABLE IF NOT EXISTS loops (
    id uuid PRIMARY KEY, prompt text NOT NULL, instruction text, model text NOT NULL,
    status text NOT NULL DEFAULT 'generating', error text, llm_response_id text,
    llm_usage jsonb, created_at timestamptz NOT NULL DEFAULT now(), generated_at timestamptz
);
CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY, loop_id uuid NOT NULL REFERENCES loops(id), worker_id text NOT NULL REFERENCES workers(id),
    status text NOT NULL DEFAULT 'queued', created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz, finished_at timestamptz, error text, result jsonb, revision text
);
CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY, loop_id uuid NOT NULL REFERENCES loops(id), run_id uuid REFERENCES runs(id),
    stage text NOT NULL, message text NOT NULL, data jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_loop_time ON events(loop_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_worker ON runs(worker_id)
    WHERE status IN ('queued', 'running');

-- Additive identity migration: legacy rows are bound by identity.py before use.
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY, username text NOT NULL UNIQUE, display_name text NOT NULL,
    password_hash text, activation_token_hash text UNIQUE,
    activation_expires_at timestamptz, activated_at timestamptz, disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspaces (
    id uuid PRIMARY KEY, slug text NOT NULL UNIQUE, name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS memberships (
    workspace_id uuid NOT NULL REFERENCES workspaces(id), user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES users(id);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS workspace_id uuid REFERENCES workspaces(id);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS workspace_id uuid REFERENCES workspaces(id);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS assigned_user_id uuid REFERENCES users(id);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS credential_hash text;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS fleet_namespace text;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS fleet_claim text;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
ALTER TABLE loops ADD COLUMN IF NOT EXISTS workspace_id uuid REFERENCES workspaces(id);
ALTER TABLE loops ADD COLUMN IF NOT EXISTS created_by uuid REFERENCES users(id);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS initiated_by uuid REFERENCES users(id);
CREATE UNIQUE INDEX IF NOT EXISTS workers_credential_hash ON workers(credential_hash);
CREATE UNIQUE INDEX IF NOT EXISTS one_worker_per_membership ON workers(workspace_id, assigned_user_id)
    WHERE disabled_at IS NULL;
CREATE INDEX IF NOT EXISTS loops_workspace_time ON loops(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS sessions_user_workspace ON sessions(user_id, workspace_id);
