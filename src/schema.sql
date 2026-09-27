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
