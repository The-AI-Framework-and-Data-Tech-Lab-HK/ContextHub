"""Drop the semantic-recompute work fencing table and token column.

Propagation is invalidation-only: an upstream change marks downstream stale and
never rewrites downstream content.  Rewriting requires a real semantic
generator and an output-validity contract, neither of which exists here, so the
two-phase recompute fencing machinery has no caller left.

This deliberately keeps the rest of revision 007 in place:
``canonical_semantic_part`` / ``context_semantic_identity`` still back the
``contexts.semantic_identity`` trigger, and ``context_invalidations.source_version``
is what lets a reader learn which upstream version invalidated a node.

Revision ID: 009
Revises: 008
Create Date: 2026-08-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_recompute_work_recovery")
    op.execute("DROP TABLE IF EXISTS propagation_recompute_work")
    op.execute("ALTER TABLE contexts DROP COLUMN IF EXISTS recompute_work_token")


def downgrade() -> None:
    op.execute("ALTER TABLE contexts ADD COLUMN recompute_work_token UUID")
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
