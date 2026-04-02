"""Initial schema — all core tables, indexes, RLS, triggers, and seed data.

Revision ID: 001
Revises:
Create Date: 2025-03-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- contexts ---
    op.execute("""
    CREATE TABLE contexts (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        uri             TEXT NOT NULL,
        context_type    TEXT NOT NULL CHECK (context_type IN ('table_schema', 'skill', 'memory', 'resource')),
        scope           TEXT NOT NULL CHECK (scope IN ('datalake', 'team', 'agent', 'user')),
        owner_space     TEXT,
        account_id      TEXT NOT NULL,
        l0_content      TEXT,
        l1_content      TEXT,
        l2_content      TEXT,
        file_path       TEXT,
        status          TEXT DEFAULT 'active' CHECK (status IN ('active', 'stale', 'archived', 'deleted', 'pending_review')),
        version         INT DEFAULT 1,
        tags            TEXT[],
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        last_accessed_at TIMESTAMPTZ DEFAULT NOW(),
        stale_at        TIMESTAMPTZ,
        archived_at     TIMESTAMPTZ,
        deleted_at      TIMESTAMPTZ,
        active_count    INT DEFAULT 0,
        adopted_count   INT DEFAULT 0,
        ignored_count   INT DEFAULT 0,
        l0_embedding    vector(1536),
        UNIQUE (account_id, uri)
    )
    """)
    op.execute("ALTER TABLE contexts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE contexts FORCE ROW LEVEL SECURITY")
    op.execute("""
    CREATE POLICY tenant_isolation ON contexts
        USING (account_id = current_setting('app.account_id'))
    """)
    op.execute("CREATE INDEX idx_contexts_scope ON contexts (scope, context_type)")
    op.execute("CREATE INDEX idx_contexts_owner ON contexts (account_id, owner_space)")
    op.execute("CREATE INDEX idx_contexts_status ON contexts (status) WHERE status != 'deleted'")
    op.execute("""
    CREATE INDEX idx_contexts_l0_embedding ON contexts
        USING hnsw (l0_embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # --- dependencies ---
    op.execute("""
    CREATE TABLE dependencies (
        id              SERIAL PRIMARY KEY,
        dependent_id    UUID NOT NULL REFERENCES contexts(id),
        dependency_id   UUID NOT NULL REFERENCES contexts(id),
        dep_type        TEXT NOT NULL CHECK (dep_type IN ('skill_version', 'table_schema', 'derived_from')),
        pinned_version  INT,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (dependent_id, dependency_id, dep_type)
    )
    """)
    op.execute("CREATE INDEX idx_deps_dependency ON dependencies (dependency_id)")
    op.execute("CREATE INDEX idx_deps_dependent ON dependencies (dependent_id)")

    # --- change_events (no RLS) ---
    op.execute("""
    CREATE TABLE change_events (
        event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        timestamp       TIMESTAMPTZ DEFAULT NOW(),
        context_id      UUID NOT NULL REFERENCES contexts(id),
        account_id      TEXT NOT NULL,
        change_type     TEXT NOT NULL CHECK (change_type IN ('created', 'modified', 'deleted', 'version_published', 'marked_stale')),
        actor           TEXT NOT NULL,
        diff_summary    TEXT,
        previous_version TEXT,
        new_version     TEXT,
        metadata        JSONB,
        delivery_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (delivery_status IN ('pending', 'processing', 'retry', 'processed')),
        attempt_count   INT NOT NULL DEFAULT 0,
        next_retry_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        claimed_at      TIMESTAMPTZ,
        processed_at    TIMESTAMPTZ,
        last_error      TEXT
    )
    """)
    op.execute("""
    CREATE INDEX idx_events_ready ON change_events (next_retry_at, timestamp)
        WHERE delivery_status IN ('pending', 'retry')
    """)
    op.execute("""
    CREATE INDEX idx_events_processing ON change_events (claimed_at)
        WHERE delivery_status = 'processing'
    """)
    op.execute("CREATE INDEX idx_events_context ON change_events (context_id)")

    # --- change_events trigger ---
    op.execute("""
    CREATE OR REPLACE FUNCTION notify_change_event() RETURNS trigger AS $$
    BEGIN
        PERFORM pg_notify('context_changed', NEW.context_id::text);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_change_events_notify
    AFTER INSERT ON change_events
    FOR EACH ROW EXECUTE FUNCTION notify_change_event()
    """)

    # --- teams ---
    op.execute("""
    CREATE TABLE teams (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        path        TEXT NOT NULL,
        parent_id   UUID REFERENCES teams(id),
        display_name TEXT,
        account_id  TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (account_id, path)
    )
    """)
    op.execute("CREATE INDEX idx_teams_parent ON teams (parent_id)")
    op.execute("CREATE INDEX idx_teams_account ON teams (account_id)")
    op.execute("ALTER TABLE teams ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teams FORCE ROW LEVEL SECURITY")
    op.execute("""
    CREATE POLICY tenant_isolation ON teams
        USING (account_id = current_setting('app.account_id'))
    """)

    # --- team_memberships ---
    op.execute("""
    CREATE TABLE team_memberships (
        agent_id    TEXT NOT NULL,
        team_id     UUID NOT NULL REFERENCES teams(id),
        role        TEXT DEFAULT 'member' CHECK (role IN ('member', 'admin')),
        access      TEXT DEFAULT 'read_write' CHECK (access IN ('read_write', 'read_only')),
        is_primary  BOOLEAN DEFAULT FALSE,
        PRIMARY KEY (agent_id, team_id)
    )
    """)

    # --- skill_versions ---
    op.execute("""
    CREATE TABLE skill_versions (
        skill_id        UUID NOT NULL REFERENCES contexts(id),
        version         INT NOT NULL,
        content         TEXT NOT NULL,
        changelog       TEXT,
        is_breaking     BOOLEAN DEFAULT FALSE,
        status          TEXT DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'deprecated')),
        published_by    TEXT,
        published_at    TIMESTAMPTZ,
        PRIMARY KEY (skill_id, version)
    )
    """)

    # --- skill_subscriptions ---
    op.execute("""
    CREATE TABLE skill_subscriptions (
        id              SERIAL PRIMARY KEY,
        agent_id        TEXT NOT NULL,
        skill_id        UUID NOT NULL REFERENCES contexts(id),
        pinned_version  INT,
        account_id      TEXT NOT NULL,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (agent_id, skill_id)
    )
    """)
    op.execute("CREATE INDEX idx_subs_skill ON skill_subscriptions (skill_id)")
    op.execute("CREATE INDEX idx_subs_agent ON skill_subscriptions (agent_id)")
    op.execute("ALTER TABLE skill_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute("""
    CREATE POLICY tenant_isolation ON skill_subscriptions
        USING (account_id = current_setting('app.account_id'))
    """)

    # --- carrier-specific: table_metadata ---
    op.execute("""
    CREATE TABLE table_metadata (
        context_id      UUID PRIMARY KEY REFERENCES contexts(id),
        catalog         TEXT NOT NULL,
        database_name   TEXT NOT NULL,
        table_name      TEXT NOT NULL,
        ddl             TEXT,
        partition_info  JSONB,
        stats           JSONB,
        sample_data     JSONB,
        stats_updated_at TIMESTAMPTZ
    )
    """)

    # --- carrier-specific: lineage ---
    op.execute("""
    CREATE TABLE lineage (
        upstream_id     UUID NOT NULL REFERENCES contexts(id),
        downstream_id   UUID NOT NULL REFERENCES contexts(id),
        transform_type  TEXT,
        description     TEXT,
        PRIMARY KEY (upstream_id, downstream_id)
    )
    """)

    # --- carrier-specific: table_relationships ---
    op.execute("""
    CREATE TABLE table_relationships (
        table_id_a      UUID NOT NULL REFERENCES contexts(id),
        table_id_b      UUID NOT NULL REFERENCES contexts(id),
        join_type       TEXT,
        join_columns    JSONB NOT NULL,
        confidence      FLOAT DEFAULT 1.0,
        PRIMARY KEY (table_id_a, table_id_b)
    )
    """)

    # --- carrier-specific: query_templates ---
    op.execute("""
    CREATE TABLE query_templates (
        id              SERIAL PRIMARY KEY,
        context_id      UUID NOT NULL REFERENCES contexts(id),
        sql_template    TEXT NOT NULL,
        description     TEXT,
        hit_count       INT DEFAULT 0,
        last_used_at    TIMESTAMPTZ,
        created_by      TEXT
    )
    """)
    op.execute("CREATE INDEX idx_qt_context ON query_templates (context_id)")

    # --- Seed data ---
    op.execute("""
    INSERT INTO teams (id, path, parent_id, display_name, account_id) VALUES
      ('00000000-0000-0000-0000-000000000001', '', NULL, '全组织', 'acme'),
      ('00000000-0000-0000-0000-000000000002', 'engineering', '00000000-0000-0000-0000-000000000001', '工程部', 'acme'),
      ('00000000-0000-0000-0000-000000000003', 'engineering/backend', '00000000-0000-0000-0000-000000000002', '后端组', 'acme'),
      ('00000000-0000-0000-0000-000000000004', 'data', '00000000-0000-0000-0000-000000000001', '数据部', 'acme'),
      ('00000000-0000-0000-0000-000000000005', 'data/analytics', '00000000-0000-0000-0000-000000000004', '数据分析组', 'acme')
    """)
    op.execute("""
    INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary) VALUES
      ('query-agent', '00000000-0000-0000-0000-000000000003', 'member', 'read_write', TRUE),
      ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE),
      ('analysis-agent', '00000000-0000-0000-0000-000000000005', 'member', 'read_write', TRUE),
      ('analysis-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
    """)


def downgrade() -> None:
    for table in [
        "query_templates", "table_relationships", "lineage", "table_metadata",
        "skill_subscriptions", "skill_versions", "team_memberships", "teams",
        "change_events", "dependencies", "contexts",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS notify_change_event() CASCADE")
