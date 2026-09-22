"""P2 canonical identity, fenced recompute work, and invalidation versions.

Revision ID: 007
Revises: 006
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Canonical whitespace is explicitly ASCII (space, tab, LF, CR, FF, VT), so
    # PostgreSQL and Python remain byte-equivalent under every locale.
    op.execute("""
    CREATE OR REPLACE FUNCTION canonical_semantic_part(value TEXT) RETURNS TEXT AS $$
      SELECT trim(both ' ' from regexp_replace(
        translate(coalesce(value, ''), E'\\n\\r\\t\\f\\v', '     '),
        ' +', ' ', 'g'
      ))
    $$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION context_semantic_identity(
      l0 TEXT, l1 TEXT, l2 TEXT
    ) RETURNS TEXT AS $$
      SELECT encode(digest(
        canonical_semantic_part(l0) || E'\\x1f' ||
        canonical_semantic_part(l1) || E'\\x1f' ||
        canonical_semantic_part(l2),
        'sha256'
      ), 'hex')
    $$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION set_context_semantic_identity() RETURNS trigger AS $$
    BEGIN
      NEW.semantic_identity := context_semantic_identity(
        NEW.l0_content, NEW.l1_content, NEW.l2_content
      );
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    UPDATE contexts
       SET semantic_identity = context_semantic_identity(
         l0_content, l1_content, l2_content
       )
    """)
    op.execute("""
    UPDATE context_versions
       SET semantic_identity = context_semantic_identity(
         l0_content, l1_content, l2_content
       )
    """)

    op.execute("""
    ALTER TABLE context_invalidations
      ADD COLUMN source_version INT
    """)
    op.execute("""
    UPDATE context_invalidations i
       SET source_version = e.source_version
      FROM change_events e
     WHERE e.event_id = i.cause_event_id
    """)

    op.execute("""
    ALTER TABLE contexts
      ADD COLUMN recompute_work_token UUID
    """)
    op.execute("""
    CREATE TABLE propagation_recompute_work (
      work_token UUID PRIMARY KEY,
      event_id UUID NOT NULL REFERENCES change_events(event_id),
      effect_key TEXT NOT NULL,
      context_id UUID NOT NULL REFERENCES contexts(id),
      account_id TEXT NOT NULL,
      expected_context_version INT NOT NULL,
      source_context_id UUID NOT NULL REFERENCES contexts(id),
      source_version INT,
      captured_causes JSONB NOT NULL,
      regeneration_input_hash TEXT NOT NULL,
      status TEXT NOT NULL CHECK (
        status IN ('generating', 'failed', 'committed', 'superseded')
      ),
      generated_payload JSONB,
      error TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE INDEX idx_recompute_work_recovery
      ON propagation_recompute_work (account_id, status, updated_at)
      WHERE status IN ('generating', 'failed')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_recompute_work_recovery")
    op.execute("DROP TABLE IF EXISTS propagation_recompute_work")
    op.execute("ALTER TABLE contexts DROP COLUMN IF EXISTS recompute_work_token")
    op.execute("ALTER TABLE context_invalidations DROP COLUMN IF EXISTS source_version")
    op.execute("DROP FUNCTION IF EXISTS context_semantic_identity(TEXT, TEXT, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS canonical_semantic_part(TEXT)")
