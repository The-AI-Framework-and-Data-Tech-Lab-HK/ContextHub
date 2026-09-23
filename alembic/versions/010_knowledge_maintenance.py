"""S2 sealed artifacts, maintenance records and append-only execution facts.

Revision ID: 010
Revises: 009 (internal revisions, not filename order)
"""

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE knowledge_artifacts (
        account_id TEXT NOT NULL,
        id UUID NOT NULL,
        kind TEXT NOT NULL,
        schema_version INT NOT NULL CHECK (schema_version = 1),
        sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (account_id, id)
    )
    """)
    op.execute("""
    CREATE TABLE maintenance_work_items (
        account_id TEXT NOT NULL,
        id UUID NOT NULL,
        snapshot_id UUID NOT NULL,
        revision INT NOT NULL CHECK (revision > 0),
        status TEXT NOT NULL CHECK (status IN ('pending','running','updated','unchanged','unresolved')),
        state JSONB NOT NULL,
        PRIMARY KEY (account_id, id),
        UNIQUE (account_id, snapshot_id),
        FOREIGN KEY (account_id, snapshot_id) REFERENCES knowledge_artifacts(account_id, id)
    )
    """)
    op.execute("""
    CREATE TABLE execution_attempts (
        account_id TEXT NOT NULL,
        call_id UUID NOT NULL,
        attempt_no INT NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
        phase TEXT NOT NULL CHECK (phase IN ('started','finished')),
        execution_id UUID NOT NULL,
        sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        payload JSONB NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (account_id, call_id, attempt_no, phase)
    )
    """)
    op.execute(
        "CREATE INDEX execution_attempts_run ON execution_attempts(account_id, execution_id)"
    )
    op.execute("""
    CREATE TABLE context_refresh_proofs (
        account_id TEXT NOT NULL,
        id UUID NOT NULL,
        target_id UUID NOT NULL,
        target_version INT NOT NULL,
        sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        payload JSONB NOT NULL,
        PRIMARY KEY (account_id, id),
        FOREIGN KEY (target_id, target_version) REFERENCES context_versions(context_id, version)
    )
    """)
    op.execute("""
    CREATE FUNCTION reject_s2_record_mutation() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'sealed_record_is_append_only' USING ERRCODE = '23514';
    END;
    $$ LANGUAGE plpgsql
    """)
    for table in (
        "knowledge_artifacts",
        "maintenance_work_items",
        "execution_attempts",
        "context_refresh_proofs",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (account_id = current_setting('app.account_id', true))
            WITH CHECK (account_id = current_setting('app.account_id', true))""")
        if table != "maintenance_work_items":
            op.execute(f"""CREATE TRIGGER immutable_s2_record BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_s2_record_mutation()""")


def downgrade():
    for table in (
        "context_refresh_proofs",
        "execution_attempts",
        "maintenance_work_items",
        "knowledge_artifacts",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION reject_s2_record_mutation()")
