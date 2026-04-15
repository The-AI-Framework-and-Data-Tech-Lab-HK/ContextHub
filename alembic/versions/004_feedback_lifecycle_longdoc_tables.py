"""Feedback, lifecycle, and long document tables for Phase 3.

Revision ID: 004
Revises: 003
Create Date: 2026-04-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check"
    )
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check
            CHECK (action IN (
                'read','write','create','update','delete','search',
                'ls','stat','promote','publish','access_denied','policy_change',
                'feedback','lifecycle_transition'
            ))
        """
    )

    op.execute(
        """
        CREATE TABLE context_feedback (
            id              BIGSERIAL PRIMARY KEY,
            context_id      UUID NOT NULL REFERENCES contexts(id),
            retrieval_id    TEXT NOT NULL,
            actor           TEXT NOT NULL,
            retrieved_at    TIMESTAMPTZ DEFAULT NOW(),
            outcome         TEXT NOT NULL CHECK (outcome IN ('adopted', 'ignored', 'corrected', 'irrelevant')),
            metadata        JSONB,
            account_id      TEXT NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX idx_feedback_idempotent
            ON context_feedback (context_id, retrieval_id, actor, account_id)
        """
    )
    op.execute("CREATE INDEX idx_feedback_context ON context_feedback (context_id)")
    op.execute(
        "CREATE INDEX idx_feedback_retrieval ON context_feedback (retrieval_id)"
    )
    op.execute("CREATE INDEX idx_feedback_account ON context_feedback (account_id)")
    op.execute("ALTER TABLE context_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE context_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON context_feedback
            USING (account_id = current_setting('app.account_id'))
        """
    )

    op.execute(
        """
        CREATE TABLE lifecycle_policies (
            context_type        TEXT NOT NULL,
            scope               TEXT NOT NULL,
            stale_after_days    INT NOT NULL DEFAULT 0 CHECK (stale_after_days >= 0),
            archive_after_days  INT NOT NULL DEFAULT 0 CHECK (archive_after_days >= 0),
            delete_after_days   INT NOT NULL DEFAULT 0 CHECK (delete_after_days >= 0),
            account_id          TEXT NOT NULL,
            updated_at          TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (account_id, context_type, scope)
        )
        """
    )
    op.execute("ALTER TABLE lifecycle_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lifecycle_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON lifecycle_policies
            USING (account_id = current_setting('app.account_id'))
        """
    )

    op.execute(
        """
        CREATE TABLE document_sections (
            section_id      SERIAL PRIMARY KEY,
            context_id      UUID NOT NULL REFERENCES contexts(id),
            parent_id       INT,
            node_id         TEXT NOT NULL,
            title           TEXT NOT NULL,
            depth           INT NOT NULL DEFAULT 0,
            start_offset    INT,
            end_offset      INT,
            summary         TEXT,
            token_count     INT,
            account_id      TEXT NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_ds_context_section ON document_sections (context_id, section_id)"
    )
    op.execute("CREATE INDEX idx_ds_context ON document_sections (context_id)")
    op.execute("CREATE INDEX idx_ds_parent ON document_sections (parent_id)")
    op.execute("CREATE INDEX idx_ds_account ON document_sections (account_id)")
    op.execute(
        """
        ALTER TABLE document_sections
            ADD CONSTRAINT fk_ds_parent_same_context
            FOREIGN KEY (context_id, parent_id)
            REFERENCES document_sections(context_id, section_id)
        """
    )
    op.execute("ALTER TABLE document_sections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_sections FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON document_sections
            USING (account_id = current_setting('app.account_id'))
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_sections CASCADE")
    op.execute("DROP TABLE IF EXISTS context_feedback CASCADE")
    op.execute("DROP TABLE IF EXISTS lifecycle_policies CASCADE")

    op.execute(
        "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check"
    )
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check
            CHECK (action IN (
                'read','write','create','update','delete','search',
                'ls','stat','promote','publish','access_denied','policy_change'
            ))
        """
    )
