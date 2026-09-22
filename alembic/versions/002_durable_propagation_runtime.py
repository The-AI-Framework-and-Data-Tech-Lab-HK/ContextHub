"""Durable P2 propagation runtime, validity, versions, and replay traces.

Revision ID: 006
Revises: 005
Create Date: 2026-08-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE contexts
      ADD COLUMN validity_status TEXT NOT NULL DEFAULT 'fresh'
        CHECK (validity_status IN (
          'fresh', 'stale', 'invalid', 'superseded', 'recomputing', 'unknown'
        )),
      ADD COLUMN validity_reason TEXT,
      ADD COLUMN semantic_identity TEXT
    """)
    op.execute("""
    UPDATE contexts
       SET validity_status = CASE
         WHEN status = 'active' THEN 'fresh'
         WHEN status = 'stale' THEN 'stale'
         ELSE 'invalid'
       END
    """)
    op.execute("""
    CREATE INDEX idx_contexts_serviceable
      ON contexts (account_id, validity_status, status)
      WHERE status != 'deleted'
    """)

    op.execute("""
    CREATE TABLE context_versions (
      context_id UUID NOT NULL REFERENCES contexts(id),
      version INT NOT NULL,
      l0_content TEXT,
      l1_content TEXT,
      l2_content TEXT,
      semantic_identity TEXT NOT NULL,
      validity_status TEXT NOT NULL DEFAULT 'fresh',
      source_event_id UUID,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (context_id, version)
    )
    """)
    op.execute("""
    INSERT INTO context_versions (
      context_id, version, l0_content, l1_content, l2_content,
      semantic_identity, validity_status
    )
    SELECT id, version, l0_content, l1_content, l2_content,
           encode(digest(
             concat_ws(E'\\x1f', coalesce(l0_content, ''), coalesce(l1_content, ''),
                       coalesce(l2_content, '')), 'sha256'
           ), 'hex'),
           validity_status
      FROM contexts
    """)
    op.execute("""
    UPDATE contexts c
       SET semantic_identity = v.semantic_identity
      FROM context_versions v
     WHERE v.context_id = c.id AND v.version = c.version
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION set_context_semantic_identity() RETURNS trigger AS $$
    BEGIN
      NEW.semantic_identity := encode(digest(
        concat_ws(E'\\x1f', coalesce(NEW.l0_content, ''), coalesce(NEW.l1_content, ''),
                  coalesce(NEW.l2_content, '')), 'sha256'
      ), 'hex');
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_context_semantic_identity
    BEFORE INSERT OR UPDATE OF l0_content, l1_content, l2_content ON contexts
    FOR EACH ROW EXECUTE FUNCTION set_context_semantic_identity()
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION snapshot_context_version() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' OR NEW.version IS DISTINCT FROM OLD.version THEN
        INSERT INTO context_versions (
          context_id, version, l0_content, l1_content, l2_content,
          semantic_identity, validity_status
        )
        VALUES (
          NEW.id, NEW.version, NEW.l0_content, NEW.l1_content, NEW.l2_content,
          NEW.semantic_identity, NEW.validity_status
        )
        ON CONFLICT (context_id, version) DO NOTHING;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_context_version_snapshot
    AFTER INSERT OR UPDATE OF version ON contexts
    FOR EACH ROW EXECUTE FUNCTION snapshot_context_version()
    """)

    op.execute("""
    CREATE TABLE context_relations (
      context_id UUID NOT NULL REFERENCES contexts(id),
      related_context_id UUID NOT NULL REFERENCES contexts(id),
      relation_type TEXT NOT NULL CHECK (
        relation_type IN ('alias_of', 'duplicate_of', 'materialized_from')
      ),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (context_id, related_context_id, relation_type)
    )
    """)
    op.execute("CREATE INDEX idx_context_relations_related ON context_relations (related_context_id)")
    op.execute("ALTER TABLE dependencies ADD COLUMN dependency_version INT")
    op.execute("""
    UPDATE dependencies d
       SET dependency_version = c.version
      FROM contexts c
     WHERE c.id = d.dependency_id
    """)

    op.execute("""
    ALTER TABLE change_events
      ADD COLUMN idempotency_key TEXT,
      ADD COLUMN source_version INT,
      ADD COLUMN graph_scope TEXT,
      ADD COLUMN parent_event_id UUID REFERENCES change_events(event_id),
      ADD COLUMN root_event_id UUID REFERENCES change_events(event_id),
      ADD COLUMN plan_id UUID,
      ADD COLUMN lease_token UUID,
      ADD COLUMN lease_owner TEXT,
      ADD COLUMN heartbeat_at TIMESTAMPTZ,
      ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      ADD COLUMN max_attempts INT NOT NULL DEFAULT 8,
      ADD COLUMN depth INT NOT NULL DEFAULT 0,
      ADD COLUMN terminal_reason TEXT
    """)
    op.execute("""
    UPDATE change_events
       SET idempotency_key = event_id::text,
           root_event_id = event_id,
           source_version = CASE
             WHEN new_version ~ '^[0-9]+$' THEN new_version::int
             ELSE NULL
           END
     WHERE idempotency_key IS NULL
    """)
    op.execute("ALTER TABLE change_events ALTER COLUMN idempotency_key SET NOT NULL")
    op.execute("""
    ALTER TABLE change_events ALTER COLUMN idempotency_key
      SET DEFAULT gen_random_uuid()::text
    """)
    op.execute("CREATE UNIQUE INDEX uq_change_events_idempotency ON change_events (account_id, idempotency_key)")
    op.execute("""
    ALTER TABLE change_events DROP CONSTRAINT change_events_delivery_status_check
    """)
    op.execute("""
    ALTER TABLE change_events ADD CONSTRAINT change_events_delivery_status_check
      CHECK (delivery_status IN (
        'pending', 'leased', 'processing', 'retry', 'processed', 'succeeded',
        'failed', 'dead_letter'
      ))
    """)
    op.execute("""
    CREATE INDEX idx_events_lease
      ON change_events (heartbeat_at, claimed_at)
      WHERE delivery_status IN ('leased', 'processing')
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION normalize_change_event() RETURNS trigger AS $$
    BEGIN
      NEW.root_event_id := COALESCE(NEW.root_event_id, NEW.event_id);
      IF NEW.source_version IS NULL THEN
        SELECT version INTO NEW.source_version FROM contexts WHERE id = NEW.context_id;
      END IF;
      NEW.updated_at := NOW();
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_change_event_runtime_defaults
    BEFORE INSERT OR UPDATE ON change_events
    FOR EACH ROW EXECUTE FUNCTION normalize_change_event()
    """)

    op.execute("""
    CREATE TABLE propagation_effects (
      event_id UUID NOT NULL REFERENCES change_events(event_id),
      effect_key TEXT NOT NULL,
      effect_type TEXT NOT NULL,
      target_context_id UUID REFERENCES contexts(id),
      source_version INT,
      status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
      lease_token UUID,
      result JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (event_id, effect_key)
    )
    """)
    op.execute("""
    CREATE TABLE context_invalidations (
      context_id UUID NOT NULL REFERENCES contexts(id),
      cause_event_id UUID NOT NULL REFERENCES change_events(event_id),
      source_context_id UUID NOT NULL REFERENCES contexts(id),
      reason_hash TEXT NOT NULL,
      resolved_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (context_id, cause_event_id)
    )
    """)
    op.execute("""
    CREATE INDEX idx_context_invalidations_open
      ON context_invalidations (context_id)
      WHERE resolved_at IS NULL
    """)
    op.execute("""
    CREATE TABLE propagation_risk_ledger (
      event_id UUID NOT NULL REFERENCES change_events(event_id),
      edge_key TEXT NOT NULL,
      plan_id UUID,
      source_version INT,
      risk_delta DOUBLE PRECISION NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (event_id, edge_key)
    )
    """)
    op.execute("""
    CREATE TABLE propagation_trace (
      trace_id BIGSERIAL PRIMARY KEY,
      event_id UUID REFERENCES change_events(event_id),
      trace_type TEXT NOT NULL,
      payload JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("CREATE INDEX idx_propagation_trace_event ON propagation_trace (event_id, trace_id)")

    op.execute("""
    CREATE TABLE retrieval_trace (
      retrieval_id UUID PRIMARY KEY,
      account_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      request_hash TEXT NOT NULL,
      candidates JSONB NOT NULL,
      final_versions JSONB NOT NULL,
      context_hash TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS normalize_change_event() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS snapshot_context_version() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_context_semantic_identity() CASCADE")
    op.execute("DROP TABLE IF EXISTS retrieval_trace")
    op.execute("DROP TABLE IF EXISTS propagation_trace")
    op.execute("DROP TABLE IF EXISTS propagation_risk_ledger")
    op.execute("DROP TABLE IF EXISTS context_invalidations")
    op.execute("DROP TABLE IF EXISTS propagation_effects")
    op.execute("DROP TABLE IF EXISTS context_relations")
    op.execute("ALTER TABLE dependencies DROP COLUMN IF EXISTS dependency_version")
    op.execute("DROP TABLE IF EXISTS context_versions")
    op.execute("DROP INDEX IF EXISTS idx_events_lease")
    op.execute("DROP INDEX IF EXISTS uq_change_events_idempotency")
    op.execute("""
    ALTER TABLE change_events DROP CONSTRAINT change_events_delivery_status_check
    """)
    op.execute("""
    UPDATE change_events
       SET delivery_status = CASE
         WHEN delivery_status = 'succeeded' THEN 'processed'
         WHEN delivery_status IN ('failed', 'dead_letter') THEN 'retry'
         WHEN delivery_status = 'leased' THEN 'processing'
         ELSE delivery_status
       END
    """)
    op.execute("""
    ALTER TABLE change_events ADD CONSTRAINT change_events_delivery_status_check
      CHECK (delivery_status IN ('pending', 'processing', 'retry', 'processed'))
    """)
    op.execute("""
    ALTER TABLE change_events
      DROP COLUMN IF EXISTS terminal_reason,
      DROP COLUMN IF EXISTS depth,
      DROP COLUMN IF EXISTS max_attempts,
      DROP COLUMN IF EXISTS updated_at,
      DROP COLUMN IF EXISTS heartbeat_at,
      DROP COLUMN IF EXISTS lease_owner,
      DROP COLUMN IF EXISTS lease_token,
      DROP COLUMN IF EXISTS plan_id,
      DROP COLUMN IF EXISTS root_event_id,
      DROP COLUMN IF EXISTS parent_event_id,
      DROP COLUMN IF EXISTS graph_scope,
      DROP COLUMN IF EXISTS source_version,
      DROP COLUMN IF EXISTS idempotency_key
    """)
    op.execute("""
    ALTER TABLE contexts
      DROP COLUMN IF EXISTS semantic_identity,
      DROP COLUMN IF EXISTS validity_reason,
      DROP COLUMN IF EXISTS validity_status
    """)
