"""ACL and audit log tables for Phase 2.

Revision ID: 003
Revises: 002
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- access_policies ---
    op.execute("""
    CREATE TABLE access_policies (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        resource_uri_pattern TEXT NOT NULL,
        principal           TEXT NOT NULL,
        effect              TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
        actions             TEXT[] NOT NULL,
        conditions          JSONB,
        field_masks         TEXT[],
        priority            INT NOT NULL DEFAULT 0,
        account_id          TEXT NOT NULL,
        created_at          TIMESTAMPTZ DEFAULT NOW(),
        updated_at          TIMESTAMPTZ DEFAULT NOW(),
        created_by          TEXT
    )
    """)
    op.execute("CREATE INDEX idx_policies_account ON access_policies (account_id)")
    op.execute("CREATE INDEX idx_policies_principal ON access_policies (principal)")
    op.execute("CREATE INDEX idx_policies_pattern ON access_policies (resource_uri_pattern)")
    op.execute("ALTER TABLE access_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE access_policies FORCE ROW LEVEL SECURITY")
    op.execute("""
    CREATE POLICY tenant_isolation ON access_policies
        USING (account_id = current_setting('app.account_id'))
    """)

    # --- audit_log ---
    op.execute("""
    CREATE TABLE audit_log (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        timestamp       TIMESTAMPTZ DEFAULT NOW(),
        actor           TEXT NOT NULL,
        action          TEXT NOT NULL,
        resource_uri    TEXT,
        context_used    TEXT[],
        result          TEXT NOT NULL CHECK (result IN ('success', 'denied', 'error')),
        metadata        JSONB,
        account_id      TEXT NOT NULL,
        ip_address      TEXT,
        request_id      UUID
    )
    """)
    op.execute("CREATE INDEX idx_audit_timestamp ON audit_log (timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_actor ON audit_log (actor)")
    op.execute("CREATE INDEX idx_audit_resource ON audit_log (resource_uri)")
    op.execute("CREATE INDEX idx_audit_account ON audit_log (account_id)")
    op.execute("CREATE INDEX idx_audit_action ON audit_log (action)")
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute("""
    CREATE POLICY tenant_isolation ON audit_log
        USING (account_id = current_setting('app.account_id'))
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS access_policies CASCADE")
