"""Document API router."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from contexthub.api.deps import (
    get_acl_service,
    get_audit_service,
    get_db,
    get_document_ingester,
    get_lifecycle_service,
    get_masking_service,
    get_request_context,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.document import (
    DocumentIngestResponse,
    DocumentSectionReadResult,
    DocumentSectionSummary,
)
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.document_ingester import LongDocumentIngester
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.post("/ingest", status_code=201)
async def ingest_document(
    uri: str = Form(...),
    tags: list[str] | None = Form(None),
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    ingester: LongDocumentIngester = Depends(get_document_ingester),
) -> DocumentIngestResponse:
    suffix = Path(file.filename or "").suffix or ".upload"
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)

        return await ingester.ingest(
            db,
            uri=uri,
            source_path=temp_path,
            ctx=ctx,
            tags=tags,
        )
    finally:
        await file.close()
        if temp_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temp_path)


@router.get("/{context_id}/sections")
async def list_document_sections(
    context_id: UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    acl: ACLService = Depends(get_acl_service),
) -> list[DocumentSectionSummary]:
    context_row = await _get_document_context(db, context_id)
    await _require_document_read_access(db, acl, context_row["uri"], ctx)

    rows = await db.fetch(
        """
        SELECT section_id, parent_id, title, depth, summary,
               start_offset, end_offset, token_count
        FROM document_sections
        WHERE context_id = $1
        ORDER BY depth ASC, section_id ASC
        """,
        context_id,
    )
    return [DocumentSectionSummary(**dict(row)) for row in rows]


@router.get("/{context_id}/section/{section_id}")
async def read_document_section(
    context_id: UUID,
    section_id: int,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    acl: ACLService = Depends(get_acl_service),
    masking: MaskingService = Depends(get_masking_service),
    lifecycle: LifecycleService = Depends(get_lifecycle_service),
    audit: AuditService = Depends(get_audit_service),
) -> DocumentSectionReadResult:
    context_row = await _get_document_context(db, context_id)
    uri = context_row["uri"]

    decision = await acl.check_read_access(db, uri, ctx)
    if not decision.allowed:
        if decision.reason in ("explicit deny", "parent team deny"):
            await audit.log_access_denied(
                ctx.account_id,
                ctx.agent_id,
                uri,
                metadata={
                    "action": "read",
                    "section_id": section_id,
                    "reason": decision.reason,
                },
            )
        raise ForbiddenError()

    row = await db.fetchrow(
        """
        SELECT section_id, title, start_offset, end_offset
        FROM document_sections
        WHERE context_id = $1 AND section_id = $2
        """,
        context_id,
        section_id,
    )
    if row is None:
        raise NotFoundError(f"Section {section_id} not found")

    start_offset = row["start_offset"]
    end_offset = row["end_offset"]
    if start_offset is None or end_offset is None:
        raise BadRequestError("Document section offsets are not available")
    if start_offset < 0 or end_offset < start_offset:
        raise BadRequestError("Document section offsets are invalid")

    extracted_path = Path(context_row["file_path"]) / "extracted.txt"
    if not extracted_path.exists():
        raise NotFoundError(f"Extracted text file not found for context {context_id}")

    # Offsets are character-based (see LongDocumentIngester.ingest), so we
    # stream-read through the file instead of loading the whole document.
    # fh.read(n) on a text-mode file returns up to n characters.
    try:
        with extracted_path.open("r", encoding="utf-8") as fh:
            if start_offset > 0:
                skipped = fh.read(start_offset)
                if len(skipped) < start_offset:
                    raise BadRequestError("Document section offsets are invalid")
            length = end_offset - start_offset
            snippet = fh.read(length) if length > 0 else ""
            if len(snippet) < length:
                raise BadRequestError("Document section offsets are invalid")
    except FileNotFoundError as exc:
        raise NotFoundError(
            f"Extracted text file not found for context {context_id}"
        ) from exc

    if context_row["status"] == "stale":
        await lifecycle.recover_from_stale(db, context_row["id"], ctx)
    else:
        await db.execute(
            "UPDATE contexts SET last_accessed_at = NOW() WHERE id = $1",
            context_row["id"],
        )

    if decision.field_masks:
        snippet = masking.apply_masks(snippet, decision.field_masks) or ""

    await audit.log_best_effort(
        db,
        ctx.agent_id,
        "read",
        uri,
        "success",
        metadata={"level": "L2", "section_id": section_id},
    )

    return DocumentSectionReadResult(
        context_id=context_id,
        section_id=row["section_id"],
        title=row["title"],
        content=snippet,
        start_offset=start_offset,
        end_offset=end_offset,
    )


async def _get_document_context(db: ScopedRepo, context_id: UUID):
    row = await db.fetchrow(
        """
        SELECT id, uri, context_type, file_path, status
        FROM contexts
        WHERE id = $1 AND status != 'deleted'
        """,
        context_id,
    )
    if row is None:
        raise NotFoundError(f"Context {context_id} not found")
    if row["context_type"] != "resource" or not row["file_path"]:
        raise BadRequestError("Context is not a long-document resource")
    return row


async def _require_document_read_access(
    db: ScopedRepo,
    acl: ACLService,
    uri: str,
    ctx: RequestContext,
):
    decision = await acl.check_read_access(db, uri, ctx)
    if not decision.allowed:
        raise ForbiddenError()
    return decision
