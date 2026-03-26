"""ReconcilerService: periodic embedding backfill scheduler."""

from __future__ import annotations

from contexthub.db.repository import PgRepository
from contexthub.services.indexer_service import IndexerService


class ReconcilerService:
    """定期扫描并补写缺失的 L0 embedding。"""

    def __init__(self, repo: PgRepository, indexer: IndexerService):
        self._repo = repo
        self._indexer = indexer

    async def reconcile_account(self, account_id: str, batch_size: int = 100) -> int:
        """补写一个租户下缺失的 embedding。返回补写数量。"""
        async with self._repo.session(account_id) as db:
            return await self._indexer.backfill_embeddings(db, batch_size)
