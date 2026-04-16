import pytest
from pydantic import ValidationError

from contexthub.errors import ServiceUnavailableError
from contexthub.models.audit import AuditAction as ServerAuditAction
from contexthub.models.feedback import CreateFeedbackRequest, FeedbackOutcome
from contexthub.models.document import DocumentIngestRequest, SectionNode
from contexthub.models.lifecycle import (
    CreateLifecyclePolicyRequest,
    LifecyclePolicy,
    LifecycleTransitionRequest,
    UpdateLifecyclePolicyRequest,
)
from contexthub.models.context import ContextStatus, ContextType, Scope
from contexthub.models.search import SearchResult as ServerSearchResult
from contexthub.models.search import SearchResponse as ServerSearchResponse
from contexthub_sdk.models import SearchResult as SdkSearchResult
from contexthub_sdk.models import AuditAction as SdkAuditAction
from contexthub_sdk.models import SearchResponse as SdkSearchResponse


def test_service_unavailable_error_maps_to_503():
    err = ServiceUnavailableError()
    assert err.status_code == 503
    assert err.detail == "Service unavailable"


def test_phase3_audit_actions_exposed_on_server_and_sdk():
    assert ServerAuditAction.LIFECYCLE_TRANSITION.value == "lifecycle_transition"
    assert ServerAuditAction.FEEDBACK.value == "feedback"
    assert SdkAuditAction.LIFECYCLE_TRANSITION.value == "lifecycle_transition"
    assert SdkAuditAction.FEEDBACK.value == "feedback"


def test_search_response_requires_non_empty_retrieval_id():
    server_result = ServerSearchResult(
        uri="ctx://test",
        context_type="memory",
        scope="agent",
        score=0.9,
        status="active",
        version=1,
    )
    sdk_result = SdkSearchResult(
        uri="ctx://test",
        context_type="memory",
        scope="agent",
        score=0.9,
        status="active",
        version=1,
    )
    assert server_result.snippet is None
    assert server_result.section_id is None
    assert server_result.retrieval_strategy is None
    assert sdk_result.snippet is None
    assert sdk_result.section_id is None
    assert sdk_result.retrieval_strategy is None

    with pytest.raises(ValidationError):
        ServerSearchResponse(results=[], total=0)
    with pytest.raises(ValidationError):
        SdkSearchResponse(results=[], total=0)
    with pytest.raises(ValidationError):
        ServerSearchResponse(results=[], total=0, retrieval_id="")
    with pytest.raises(ValidationError):
        SdkSearchResponse(results=[], total=0, retrieval_id="")

    server_resp = ServerSearchResponse(
        results=[],
        total=0,
        retrieval_id="550e8400-e29b-41d4-a716-446655440000",
    )
    sdk_resp = SdkSearchResponse(
        results=[],
        total=0,
        retrieval_id="550e8400-e29b-41d4-a716-446655440000",
    )
    assert server_resp.retrieval_id == "550e8400-e29b-41d4-a716-446655440000"
    assert sdk_resp.retrieval_id == "550e8400-e29b-41d4-a716-446655440000"


def test_phase3_models_preserve_runtime_enums_and_json_dump_values():
    feedback_req = CreateFeedbackRequest(
        context_uri="ctx://team/eng/design-doc",
        outcome=FeedbackOutcome.ADOPTED,
        retrieval_id="550e8400-e29b-41d4-a716-446655440000",
    )
    feedback_row = CreateFeedbackRequest(
        context_uri="ctx://team/eng/design-doc",
        outcome=FeedbackOutcome.IGNORED,
    )
    lifecycle_req = CreateLifecyclePolicyRequest(
        context_type=ContextType.MEMORY,
        scope=Scope.AGENT,
    )
    transition_req = LifecycleTransitionRequest(
        context_uri="ctx://team/eng/design-doc",
        target_status=ContextStatus.ARCHIVED,
    )

    assert feedback_req.outcome is FeedbackOutcome.ADOPTED
    assert feedback_row.outcome is FeedbackOutcome.IGNORED
    assert lifecycle_req.context_type is ContextType.MEMORY
    assert lifecycle_req.scope is Scope.AGENT
    assert transition_req.target_status is ContextStatus.ARCHIVED

    assert feedback_req.model_dump(mode="json") == {
        "context_uri": "ctx://team/eng/design-doc",
        "outcome": "adopted",
        "retrieval_id": "550e8400-e29b-41d4-a716-446655440000",
        "metadata": None,
    }
    assert lifecycle_req.model_dump(mode="json")["context_type"] == "memory"
    assert lifecycle_req.model_dump(mode="json")["scope"] == "agent"
    assert transition_req.model_dump(mode="json")["target_status"] == "archived"


def test_lifecycle_models_enforce_non_negative_day_values():
    policy = LifecyclePolicy(
        context_type="memory",
        scope="agent",
        stale_after_days=1,
        archive_after_days=0,
        delete_after_days=30,
        account_id="acme",
    )
    assert policy.delete_after_days == 30

    create_req = CreateLifecyclePolicyRequest(context_type="memory", scope="agent")
    update_req = UpdateLifecyclePolicyRequest(archive_after_days=7)
    assert create_req.archive_after_days == 0
    assert update_req.archive_after_days == 7
    assert create_req.context_type is ContextType.MEMORY
    assert create_req.scope is Scope.AGENT
    assert create_req.model_dump(mode="json")["context_type"] == "memory"
    assert create_req.model_dump(mode="json")["scope"] == "agent"

    with pytest.raises(ValidationError):
        CreateLifecyclePolicyRequest(
            context_type="memory",
            scope="agent",
            stale_after_days=-1,
        )


def test_document_models_support_recursive_section_tree():
    node = SectionNode(
        node_id="0001",
        title="Root",
        children=[SectionNode(node_id="0001.1", parent_node_id="0001", title="Child")],
    )
    assert node.children[0].parent_node_id == "0001"


def test_document_ingest_request_accepts_optional_tags():
    req = DocumentIngestRequest(uri="ctx://docs/manual", tags=["handbook"])
    assert req.tags == ["handbook"]
