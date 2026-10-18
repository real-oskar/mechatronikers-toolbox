-- Central store of the hub: schema of docs/13 "Postgres schema sketch" (D72, D79-D82, D87, D88).
--
-- Conventions
-- * Every table carries org_id (one default org until OIDC, D73/D82). Run-scoped tables reference
--   runs (id, org_id), so a row can never sit in another org than its run.
-- * Ids are text with the existing prefixes (run-, ses-, build-, snap-); the charset is the one the
--   review store already enforces for file names (review_app/store.py check_id).
-- * Times are timestamptz. Enumerations are CHECK constraints on text, so adding a value is a
--   one-line migration. Pydantic payloads (GateRequest, GateDecision, ReviewItem, Event) are kept
--   whole as JSONB next to the columns the hub filters on.
-- * Users and memberships come with OIDC (D82); until then created_by / decided_by hold the
--   reviewer name entered in the browser.

CREATE DOMAIN mt_id AS text CHECK (VALUE ~ '^[A-Za-z0-9._-]{1,100}$');
CREATE DOMAIN mt_run_kind AS text CHECK (VALUE IN ('part', 'circuit'));
CREATE DOMAIN mt_retention AS text CHECK (VALUE IN ('ephemeral', 'standard', 'audit'));  -- D81
CREATE DOMAIN mt_consent AS text CHECK (VALUE IN ('training_allowed', 'eval_only', 'not_allowed'));
CREATE DOMAIN mt_sha256 AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN mt_eur AS numeric(12, 6) CHECK (VALUE >= 0);  -- negative costs are rejected (docs/13 §1)
-- Blob keys as LocalBlobStore accepts them (docs/13): no empty, "." or ".." segments.
CREATE DOMAIN mt_blob_key AS text
    CHECK (VALUE ~ '^[a-z]+(/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127})+$');


-- Organisations and workers ------------------------------------------------------------------

CREATE TABLE orgs (
    id mt_id PRIMARY KEY,
    name text NOT NULL,
    max_active_runs integer CHECK (max_active_runs >= 0),  -- D87; null = no limit
    budget_eur mt_eur,                                     -- D87; null = no limit
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO orgs (id, name) VALUES ('org-default', 'Default organisation');

CREATE TABLE workers (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    name text NOT NULL,
    slots integer NOT NULL DEFAULT 1 CHECK (slots >= 0),  -- MT_WORKER_SLOTS (D87)
    version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz,  -- heartbeat
    UNIQUE (id, org_id)
);


-- Test bench (#21, doc 09) ------------------------------------------------------------------

CREATE TABLE testcases (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    kind mt_run_kind NOT NULL,
    name text NOT NULL,
    drawing_key mt_blob_key NOT NULL,
    reference_key mt_blob_key,  -- readable by the test bench role only, never handed to a worker
    meta jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(meta) = 'object'),  -- source, difficulty
    -- Which licence and consent metadata an upload must carry is open point P10.
    licence text,
    consent mt_consent,
    retention_class mt_retention NOT NULL DEFAULT 'audit',  -- test cases are audit (D81)
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    CHECK (starts_with(drawing_key, 'testcases/' || id || '/')),
    CHECK (starts_with(reference_key, 'testcases/' || id || '/'))
);

CREATE TABLE testsets (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    name text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    UNIQUE (org_id, name, version)
);

CREATE TABLE testset_cases (
    testset_id mt_id NOT NULL,
    case_id mt_id NOT NULL,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (testset_id, case_id),
    UNIQUE (testset_id, position),
    FOREIGN KEY (testset_id, org_id) REFERENCES testsets (id, org_id),
    FOREIGN KEY (case_id, org_id) REFERENCES testcases (id, org_id)
);


-- Plans, batches, runs and sessions -----------------------------------------------------------

CREATE TABLE plans (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    kind mt_run_kind NOT NULL,
    file_name text NOT NULL,
    input_artifact_id mt_id,  -- foreign key below: artifacts reference runs, runs reference plans
    source text NOT NULL CHECK (source IN ('upload', 'testcase', 'connector')),
    testcase_id mt_id,
    -- No default: the default consent per data source is open point P8; the hub sets it.
    consent mt_consent NOT NULL,
    retention_class mt_retention NOT NULL DEFAULT 'standard',
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    FOREIGN KEY (testcase_id, org_id) REFERENCES testcases (id, org_id),
    CHECK ((source = 'testcase') = (testcase_id IS NOT NULL))
);

CREATE TABLE batches (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    name text NOT NULL,
    testset_id mt_id,
    options jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(options) = 'object'),
    -- Accepted from the test bench token on a test-set batch only (D57, docs/13 §7).
    auto_approve boolean NOT NULL DEFAULT false,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    FOREIGN KEY (testset_id, org_id) REFERENCES testsets (id, org_id),
    CHECK (NOT auto_approve OR testset_id IS NOT NULL)
);

-- One plan in one sandbox. The hub run id is the driver's run id (LangGraph thread id). Queued
-- runs are the work queue (D87): a worker claims the oldest with
--   SELECT id FROM runs WHERE org_id = $1 AND state = 'queued'
--   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
-- and sets state 'claimed' in the same transaction; runs_queue keeps that an index scan.
CREATE TABLE runs (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    plan_id mt_id,  -- null only for a run mirrored from a mode 1 session (docs/13 step 5)
    kind mt_run_kind NOT NULL DEFAULT 'part',
    batch_id mt_id,
    session_id mt_id,
    worker_id mt_id,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'claimed', 'running', 'awaiting_review', 'done', 'failed', 'aborted', 'cancelled'
    )),
    phase text,  -- cached RunSummary.phase (session/runs.py rules), from trusted events only
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    model text,
    options jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(options) = 'object'),
    auto_approve boolean NOT NULL DEFAULT false,  -- copied from a test-bench batch (D57)
    retention_class mt_retention NOT NULL DEFAULT 'standard',
    -- Settled from the worker's run.settled report, never summed from model.response (docs/13 §1).
    cost_eur mt_eur NOT NULL DEFAULT 0,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    last_activity_at timestamptz,
    UNIQUE (id, org_id),
    FOREIGN KEY (plan_id, org_id) REFERENCES plans (id, org_id),
    FOREIGN KEY (batch_id, org_id) REFERENCES batches (id, org_id),
    FOREIGN KEY (worker_id, org_id) REFERENCES workers (id, org_id),
    CHECK (NOT auto_approve OR batch_id IS NOT NULL)
);
CREATE INDEX runs_queue ON runs (org_id, created_at, id) WHERE state = 'queued';
CREATE INDEX runs_overview ON runs (org_id, last_activity_at DESC NULLS LAST, id);
CREATE INDEX runs_active ON runs (org_id) WHERE state IN ('claimed', 'running', 'awaiting_review');
CREATE INDEX runs_batch ON runs (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX runs_session ON runs (session_id) WHERE session_id IS NOT NULL;

-- Derived from session.* events of trusted emitters only (docs/13 §4). `record` is the
-- SessionRecord as saved (RunRepository, #72) with its timestamps as sent; the columns are copies
-- for filters. The per-session gateway token never leaves the worker host (docs/13 §5).
CREATE TABLE sessions (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    run_id mt_id,
    worker_id mt_id,
    state text NOT NULL CHECK (state IN (
        'created', 'running', 'awaiting_review', 'snapshotting', 'shelved', 'failed', 'expired',
        'deleted'
    )),
    snapshot_id mt_id,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object' AND NOT record ? 'token'),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    FOREIGN KEY (worker_id, org_id) REFERENCES workers (id, org_id)
);
CREATE INDEX sessions_run ON sessions (run_id);


-- Events (docs/08, provenance rules of docs/13 §1) -------------------------------------------

-- Append-only log; id is the SSE id (EventRepository, #72). No foreign key on run_id: a forwarded
-- line may arrive before its run row (mode 1 mirror) and the table is partitioned by month later.
-- Which lines count for state (provenance rules) is decided by the readers from source and type.
CREATE TABLE events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    session_id mt_id,
    run_id mt_id,
    -- The hub's ingest rejects lines without one (docs/13); stored ones are never de-duplicated.
    emitter_id text CHECK (length(emitter_id) BETWEEN 1 AND 100),
    seq bigint NOT NULL,
    -- Where the line was read: a trusted host process, or container stdout (untrusted, the default).
    source text NOT NULL DEFAULT 'container' CHECK (source IN ('host', 'container')),
    component text NOT NULL,
    type text NOT NULL,
    level text NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    ts text NOT NULL,  -- as sent: the emitter's clock (sandbox clocks drift, T13), untrusted
    ingested_at timestamptz NOT NULL DEFAULT now(),  -- hub clock
    body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),  -- the Event as ingested
    UNIQUE (emitter_id, seq)
);
CREATE INDEX events_session ON events (session_id, id);
CREATE INDEX events_run ON events (run_id, id);
CREATE INDEX events_type ON events (type);

-- Where reading a host-side source stopped (EventRepository cursors, #72).
CREATE TABLE event_cursors (
    org_id mt_id NOT NULL REFERENCES orgs (id),
    source text NOT NULL,
    last_ts text NOT NULL,
    PRIMARY KEY (org_id, source)
);


-- Review state (D62, D80): gates, decisions, corrections, findings ---------------------------

CREATE TABLE gates (
    run_id mt_id NOT NULL,
    id mt_id NOT NULL,  -- stable per run: LangGraph interrupt id (part), gate-<run_id>-1 (circuit)
    org_id mt_id NOT NULL REFERENCES orgs (id),
    kind text NOT NULL CHECK (kind IN ('spec_review', 'result_review', 'gave_up')),
    run_kind mt_run_kind NOT NULL DEFAULT 'part',
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    -- GateState as saved (ReviewRepository, #72): the request and the decision, set once
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    decision jsonb CHECK (jsonb_typeof(decision) = 'object'),
    opened_at timestamptz NOT NULL DEFAULT now(),  -- hub clock, like decided_at
    action text,
    decided_by text,
    decided_at timestamptz,
    PRIMARY KEY (run_id, id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    CHECK (
        (decision IS NULL) = (action IS NULL)
        AND (decision IS NULL) = (decided_by IS NULL)
        AND (decision IS NULL) = (decided_at IS NULL)
    ),
    CHECK (  -- spec.review.ACTIONS
        action IS NULL
        OR (kind = 'spec_review' AND action IN ('approve', 'reject'))
        OR (kind = 'result_review' AND action IN ('finish', 'update'))
        OR (kind = 'gave_up' AND action IN ('retry', 'abort'))
    )
);
CREATE INDEX gates_open ON gates (run_id) WHERE decision IS NULL;

-- One writer, one decision (D80, D62): the request never changes and a decision is final, also
-- for a writer that skips the review API's 409 check.
CREATE FUNCTION mt_gate_decided_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.run_id, NEW.id, NEW.org_id, NEW.kind, NEW.run_kind, NEW.revision, NEW.request,
        NEW.opened_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.id, OLD.org_id, OLD.kind, OLD.run_kind, OLD.revision, OLD.request,
        OLD.opened_at) THEN
        RAISE EXCEPTION 'gate % of run %: the request cannot change', OLD.id, OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.decision IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'gate % of run % is already decided', OLD.id, OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER gates_decided_once BEFORE UPDATE ON gates
    FOR EACH ROW EXECUTE FUNCTION mt_gate_decided_once();

-- GateDecision.corrections (D84) as rows, written with the decision: what the run driver applies
-- through circuit-mcp (#57), and what dataset export reads.
CREATE TABLE gate_corrections (
    run_id mt_id NOT NULL,
    gate_id mt_id NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    org_id mt_id NOT NULL REFERENCES orgs (id),
    target text NOT NULL CHECK (target IN ('component', 'terminal', 'net')),
    target_id text NOT NULL,  -- refdes, terminal id ("Q1.2") or net id
    field text NOT NULL,
    old_value text,
    new_value text NOT NULL,
    PRIMARY KEY (run_id, gate_id, position),
    FOREIGN KEY (run_id, gate_id) REFERENCES gates (run_id, id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id)
);

-- Points of interest: `record` is the StoredItem as saved (ReviewRepository, #72; ids
-- rev<n>-<agent item id> or poi-<hex>); the columns are copies for the overview's open-POI count.
CREATE TABLE findings (
    run_id mt_id NOT NULL,
    id mt_id NOT NULL,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    revision integer NOT NULL CHECK (revision >= 1),
    origin text NOT NULL CHECK (origin IN ('agent', 'reviewer')),
    kind text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'approved', 'edited', 'rejected', 'resolved')),
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id)
);
CREATE INDEX findings_open ON findings (run_id, revision) WHERE status = 'open';

-- Audit trail of POI actions; append-only.
CREATE TABLE finding_actions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id mt_id NOT NULL,
    finding_id mt_id NOT NULL,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    action text NOT NULL CHECK (action IN ('approved', 'edited', 'rejected', 'resolved', 'note')),
    reviewer text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    comment text,
    at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, finding_id) REFERENCES findings (run_id, id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id)
);
CREATE INDEX finding_actions_finding ON finding_actions (run_id, finding_id, id);

CREATE FUNCTION mt_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END
$$;
CREATE TRIGGER finding_actions_append_only BEFORE UPDATE OR DELETE ON finding_actions
    FOR EACH ROW EXECUTE FUNCTION mt_append_only();


-- Artifacts: the index of the blob store (docs/13 "Blob store interface") --------------------

CREATE TABLE artifacts (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    run_id mt_id,
    session_id mt_id,
    build_id mt_id,
    revision integer CHECK (revision >= 1),
    role text NOT NULL CHECK (role IN (
        'input', 'sheet', 'spec', 'export', 'netlist', 'report', 'snapshot', 'log'
    )),
    format text,  -- step, stl, glb, png, json, net, …
    name text NOT NULL,
    blob_key mt_blob_key NOT NULL UNIQUE,  -- derived by the hub, never chosen by a worker
    content_type text NOT NULL,
    sha256 mt_sha256 NOT NULL,
    bytes bigint NOT NULL CHECK (bytes >= 0),
    retention_class mt_retention NOT NULL DEFAULT 'standard',
    -- Set by the hub from the class (D81: 1, 30 or 365 days); every class expires, audit included,
    -- so the retention job finds every row.
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,  -- blob removed by the retention job; the row keeps its metadata (D81)
    UNIQUE (id, org_id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    -- inputs/ is the only prefix a worker may copy into a sandbox, and holds inputs only;
    -- testcases/ (references) is never a run's artifact (docs/13, #21).
    CHECK (starts_with(blob_key, 'inputs/') = (role = 'input')),
    CHECK (NOT starts_with(blob_key, 'testcases/'))
);
CREATE INDEX artifacts_run ON artifacts (run_id, revision, role);
CREATE INDEX artifacts_expiry ON artifacts (expires_at) WHERE deleted_at IS NULL;

ALTER TABLE plans ADD FOREIGN KEY (input_artifact_id, org_id) REFERENCES artifacts (id, org_id)
    DEFERRABLE INITIALLY DEFERRED;  -- plan, run and input artifact are created in one transaction


-- Worker commands, budget, bench results ------------------------------------------------------

-- Pulled by workers like the run queue (FOR UPDATE SKIP LOCKED on commands_pending).
CREATE TABLE commands (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    run_id mt_id NOT NULL,
    kind text NOT NULL CHECK (kind IN ('shelve', 'restore', 'cancel')),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'claimed', 'done', 'failed')),
    requested_by text NOT NULL,
    confirmed_at timestamptz,  -- shelving needs the user's confirmed OK (D22)
    worker_id mt_id,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    done_at timestamptz,
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    FOREIGN KEY (worker_id, org_id) REFERENCES workers (id, org_id),
    CHECK (kind <> 'shelve' OR confirmed_at IS NOT NULL)
);
CREATE INDEX commands_pending ON commands (org_id, created_at, id) WHERE state = 'pending';

-- Budget reservation at claim, settlement from the worker's report (D87).
CREATE TABLE spend (
    run_id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    reserved_eur mt_eur NOT NULL,
    spent_eur mt_eur,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    CHECK ((spent_eur IS NULL) = (settled_at IS NULL))
);

CREATE TABLE bench_results (
    run_id mt_id NOT NULL,
    case_id mt_id NOT NULL,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    testset_id mt_id,
    metrics jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metrics) = 'object'),
    solved boolean,  -- D91; null until compared
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, case_id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    FOREIGN KEY (case_id, org_id) REFERENCES testcases (id, org_id),
    FOREIGN KEY (testset_id, org_id) REFERENCES testsets (id, org_id)
);


-- Dataset records (doc 11, V2) ----------------------------------------------------------------

CREATE TABLE dataset_records (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    task text NOT NULL CHECK (task IN (
        'label_reading', 'spec_extraction', 'feature_planning', 'repair'
    )),
    input_refs jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(input_refs) = 'array'),
    target jsonb NOT NULL,  -- human-approved output only (doc 11 rule 2)
    model_output jsonb,
    corrections jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(corrections) = 'array'),
    source_run mt_id,
    source_session mt_id,
    source_snapshot mt_id,
    split text NOT NULL CHECK (split IN ('train', 'val', 'eval', 'excluded')),
    consent mt_consent NOT NULL,  -- no default: the default per data source is open point P8
    curation text NOT NULL DEFAULT 'uncurated'
        CHECK (curation IN ('uncurated', 'gold', 'usable', 'rejected')),
    curated_by text,
    curated_at timestamptz,
    retention_class mt_retention NOT NULL DEFAULT 'standard',
    expires_at timestamptz,  -- not later than the source's retention (doc 11 rule 4)
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_run, org_id) REFERENCES runs (id, org_id),
    CHECK (source_run IS NOT NULL OR source_session IS NOT NULL),  -- traceability (rule 5)
    CHECK (split NOT IN ('train', 'val') OR consent = 'training_allowed'),
    CHECK (
        (curation = 'uncurated') = (curated_by IS NULL)
        AND (curated_by IS NULL) = (curated_at IS NULL)
    )
);
CREATE INDEX dataset_records_queue ON dataset_records (org_id, task, created_at)
    WHERE curation = 'uncurated';

-- No training on test data (D32): a record from a test-case plan or a test-set batch stays eval,
-- whether it names the run or only the run's session.
CREATE FUNCTION mt_dataset_eval_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.split <> 'eval' AND EXISTS (
        SELECT 1
        FROM runs r
        LEFT JOIN plans p ON p.id = r.plan_id
        LEFT JOIN batches b ON b.id = r.batch_id
        WHERE (
            r.id = NEW.source_run
            OR r.session_id = NEW.source_session
            OR r.id IN (SELECT s.run_id FROM sessions s WHERE s.id = NEW.source_session)
        )
        AND (p.source = 'testcase' OR b.testset_id IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'dataset record %: runs of test cases stay in split eval (D32)', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER dataset_records_eval_only BEFORE INSERT OR UPDATE ON dataset_records
    FOR EACH ROW EXECUTE FUNCTION mt_dataset_eval_only();


-- Tokens (D73, D88) ---------------------------------------------------------------------------

-- Random bearer tokens, stored as SHA-256 only. Roles: the shared browser credential (D73), worker
-- tokens, per-run tokens minted at claim, the test bench, and per-client MCP tokens (D88).
CREATE TABLE api_tokens (
    id mt_id PRIMARY KEY,
    org_id mt_id NOT NULL REFERENCES orgs (id),
    role text NOT NULL CHECK (role IN ('shared', 'worker', 'run', 'testbench', 'mcp_client')),
    token_sha256 mt_sha256 NOT NULL UNIQUE,
    label text,
    worker_id mt_id,
    run_id mt_id,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz,
    FOREIGN KEY (worker_id, org_id) REFERENCES workers (id, org_id),
    FOREIGN KEY (run_id, org_id) REFERENCES runs (id, org_id),
    CHECK ((role = 'worker') = (worker_id IS NOT NULL)),
    CHECK ((role = 'run') = (run_id IS NOT NULL))
);
