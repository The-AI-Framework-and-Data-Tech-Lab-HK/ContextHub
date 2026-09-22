"""Keep context-version snapshots synchronized with final validity.

Revision ID: 008
Revises: 007
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION snapshot_context_version() RETURNS trigger AS $$
    BEGIN
      INSERT INTO context_versions (
        context_id, version, l0_content, l1_content, l2_content,
        semantic_identity, validity_status
      )
      VALUES (
        NEW.id, NEW.version, NEW.l0_content, NEW.l1_content, NEW.l2_content,
        NEW.semantic_identity, NEW.validity_status
      )
      ON CONFLICT (context_id, version) DO UPDATE
        SET l0_content = EXCLUDED.l0_content,
            l1_content = EXCLUDED.l1_content,
            l2_content = EXCLUDED.l2_content,
            semantic_identity = EXCLUDED.semantic_identity,
            validity_status = EXCLUDED.validity_status;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_context_version_snapshot ON contexts")
    op.execute("""
    CREATE TRIGGER trg_context_version_snapshot
    AFTER INSERT OR UPDATE OF version, validity_status ON contexts
    FOR EACH ROW EXECUTE FUNCTION snapshot_context_version()
    """)
    op.execute("""
    UPDATE context_versions v
       SET l0_content = c.l0_content,
           l1_content = c.l1_content,
           l2_content = c.l2_content,
           semantic_identity = c.semantic_identity,
           validity_status = c.validity_status
      FROM contexts c
     WHERE c.id = v.context_id AND c.version = v.version
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_context_version_snapshot ON contexts")
    op.execute("""
    CREATE TRIGGER trg_context_version_snapshot
    AFTER INSERT OR UPDATE OF version ON contexts
    FOR EACH ROW EXECUTE FUNCTION snapshot_context_version()
    """)
