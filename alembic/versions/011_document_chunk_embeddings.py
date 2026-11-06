"""Version-bound original document chunk embeddings; no historical backfill."""
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE document_chunk_embeddings (
            account_id TEXT NOT NULL,
            context_id UUID NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
            context_version INT NOT NULL,
            section_id INT NOT NULL,
            start_offset INT NOT NULL CHECK (start_offset >= 0),
            end_offset INT NOT NULL CHECK (end_offset > start_offset),
            source_sha256 TEXT NOT NULL,
            embedding vector(1536) NOT NULL,
            PRIMARY KEY (context_id, context_version, start_offset, end_offset),
            FOREIGN KEY (context_id, section_id)
                REFERENCES document_sections(context_id, section_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_document_chunks_embedding ON document_chunk_embeddings USING hnsw (embedding vector_cosine_ops)")
    op.execute("ALTER TABLE document_chunk_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_chunk_embeddings FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON document_chunk_embeddings
        USING (account_id = current_setting('app.account_id'))""")


def downgrade():
    op.execute("DROP TABLE document_chunk_embeddings")
