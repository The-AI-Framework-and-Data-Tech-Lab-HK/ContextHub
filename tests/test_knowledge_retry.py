import asyncio

import httpx
import pytest
from pydantic import ValidationError

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import (
    RecordedCall,
    ResponseParseError,
    ResponseSchemaError,
)
from contexthub.models.knowledge import ContractError
from knowledge_helpers import MemorySink, identity, policy


def success(request, tokens=7):
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": str(tokens)},
        json={
            "model": "model-snapshot",
            "usage": {"prompt_tokens": tokens, "completion_tokens": 2},
            "choices": [{"message": {"content": "ok"}}],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, "timeout", "connect"])
async def test_retry_each_failure_is_recorded(failure):
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("secret must not be logged")
            if failure == "connect":
                raise httpx.ConnectError("secret must not be logged")
            return httpx.Response(failure, request=request, json={})
        return success(request)

    sink = MemorySink()
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        assert (
            await client.complete(
                "prompt", call=RecordedCall(identity(), policy(), sink)
            )
            == "ok"
        )
        assert [r.phase for r in sink.rows] == [
            "started",
            "finished",
            "started",
            "finished",
        ]
        first, last = sink.rows[1], sink.rows[3]
        assert first.error.retryable and first.usage.completeness == "missing"
        assert first.cost.amount is None and first.stop_reason == "retry_scheduled"
        assert last.actual_model == "model-snapshot" and last.request_id == "7"
        assert last.usage.input_tokens == 7
        assert "secret must" not in "".join(r.model_dump_json() for r in sink.rows)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exhaustion_outer_retry_and_same_call_concurrency():
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(503, request=request, json={})

    sink = MemorySink()
    call = RecordedCall(identity(), policy(), sink)
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", call=call)
        assert count == 3 and sink.rows[-1].stop_reason == "retry_exhausted"
        with pytest.raises(ContractError):
            await client.complete("x", call=call)
        assert count == 3 and len(sink.rows) == 6
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "schema", "validator", 400])
async def test_deterministic_errors_never_retry(failure):
    def handle(request):
        if failure == "json":
            return httpx.Response(200, request=request, text="{bad")
        if failure == "schema":
            return httpx.Response(200, request=request, json={"choices": []})
        if failure == 400:
            return httpx.Response(400, request=request, json={})
        return success(request)

    def validate(_):
        raise ResponseSchemaError("invalid output schema")

    sink = MemorySink()
    call = RecordedCall(
        identity(),
        policy(),
        sink,
        validate_response=validate if failure == "validator" else None,
    )
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(
            (ResponseParseError, ResponseSchemaError, httpx.HTTPStatusError)
        ):
            await client.complete("x", call=call)
        assert len(sink.rows) == 2 and sink.rows[-1].stop_reason == "non_retryable"
        if failure == "validator":
            assert sink.rows[-1].usage.input_tokens == 7
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_mix_usage():
    async def handle(request):
        import json

        tokens = int(json.loads(request.content)["messages"][0]["content"])
        await asyncio.sleep(0 if tokens == 19 else 0.01)
        return success(request, tokens)

    sink = MemorySink()
    a, b = identity(), identity()
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        await asyncio.gather(
            *(
                client.complete(str(n), call=RecordedCall(i, policy(), sink))
                for n, i in [(7, a), (19, b)]
            )
        )
        finals = {r.identity.call_id: r for r in sink.rows if r.phase == "finished"}
        assert finals[a.call_id].usage.input_tokens == 7
        assert finals[b.call_id].usage.input_tokens == 19
        assert finals[a.call_id].input_sha256 != finals[b.call_id].input_sha256
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_and_recorder_failure():
    sink = MemorySink()

    async def cancel(request):
        raise asyncio.CancelledError()

    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(cancel)
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.complete("x", call=RecordedCall(identity(), policy(), sink))
        assert (
            sink.rows[-1].outcome == "cancelled" and sink.rows[-1].cost.amount is None
        )
    finally:
        await client.close()
    calls = []

    class Broken:
        async def append(self, record):
            raise OSError("disk failure")

    client = OpenAIChatClient(
        "test",
        model="fixture",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    try:
        with pytest.raises(OSError):
            await client.complete(
                "x", call=RecordedCall(identity(), policy(), Broken())
            )
        assert calls == []
    finally:
        await client.close()


def test_policy_is_bounded_and_explicit():
    for kwargs in (
        {"max_attempts": 4},
        {"retry_status_codes": (400,)},
        {"backoff_seconds": ()},
        {"retry_transport_errors": ("ValueError",)},
        {"timeout_seconds": 0},
    ):
        with pytest.raises(ValidationError):
            policy(**kwargs)


@pytest.mark.asyncio
async def test_recorded_call_requires_explicit_model_and_missing_metadata_stays_missing():
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "completion-not-request",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 99,
                },
            },
        )

    sink = MemorySink()
    client = OpenAIChatClient("test", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ContractError, match="recorded_call_model_required"):
            await client.complete("x", call=RecordedCall(identity(), policy(), sink))
        assert not sent and not sink.rows
    finally:
        await client.close()
    client = OpenAIChatClient(
        "test", model="explicit-model", transport=httpx.MockTransport(handle)
    )
    try:
        await client.complete("x", call=RecordedCall(identity(), policy(), sink))
        final = sink.rows[-1]
        assert (
            final.actual_model is None
            and final.actual_model_missing_reason == "provider_not_returned"
        )
        assert (
            final.request_id is None
            and final.request_id_missing_reason == "provider_not_returned"
        )
        assert final.usage.completeness == "inconsistent" and final.cost.amount is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_finish_record_blocks_further_sends():
    class FailsFinish(MemorySink):
        async def append(self, record):
            if record.phase == "finished":
                raise OSError("ledger temporarily unavailable")
            await super().append(record)

    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(503, request=request, json={})

    sink = FailsFinish()
    call = RecordedCall(identity(), policy(), sink)
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(OSError):
            await client.complete("x", call=call)
        assert len(sent) == 1 and [r.phase for r in sink.rows] == ["started"]
        with pytest.raises(ContractError):
            await client.complete("x", call=call)
        assert len(sent) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_injected_backoff_timeout_and_call_input_identity():
    from contexthub.services.execution_ledger import validate_append

    calls = []
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, request=request, json={})
        return success(request)

    sink = MemorySink()
    configured = policy(max_attempts=2, backoff_seconds=(0.25,), timeout_seconds=2.5)
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        await client.complete(
            "x", call=RecordedCall(identity(), configured, sink, sleep=sleep)
        )
        assert delays == [0.25] and calls[0].extensions["timeout"]["read"] == 2.5
        with pytest.raises(ContractError, match="call_identity_mismatch"):
            validate_append(
                sink.rows[:2],
                sink.rows[2].model_copy(update={"input_sha256": "0" * 64}),
            )
        assert sink.rows[2].attempt_no == 2 and sink.rows[3].stop_reason == "success"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["", None, "provider-request-123", " \t"])
@pytest.mark.parametrize("scenario", ["success", "retry_success", "exhausted"])
async def test_optional_request_id_preserves_each_attempt_usage(request_id, scenario):
    sent = []

    def handle(request):
        sent.append(request)
        number = len(sent)
        status = (
            200
            if scenario == "success" or (scenario == "retry_success" and number == 2)
            else 503
        )
        return httpx.Response(
            status,
            request=request,
            headers={} if request_id is None else {"x-request-id": request_id},
            json={
                "id": "completion-is-not-request-id",
                "model": "model-snapshot",
                "usage": {
                    "prompt_tokens": 10 * number,
                    "completion_tokens": 5 * number,
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    sink = MemorySink()
    call = RecordedCall(identity(), policy(), sink)
    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    try:
        if scenario == "exhausted":
            with pytest.raises(httpx.HTTPStatusError):
                await client.complete("x", call=call)
        else:
            assert await client.complete("x", call=call) == "ok"
        expected = {"success": 1, "retry_success": 2, "exhausted": 3}[scenario]
        assert len(sent) == expected
        assert [row.phase for row in sink.rows] == ["started", "finished"] * expected
        finals = sink.rows[1::2]
        for number, row in enumerate(finals, start=1):
            success = scenario != "exhausted" and number == expected
            assert row.attempt_no == number
            assert row.status_code == (200 if success else 503)
            assert row.outcome == ("success" if success else "error")
            assert row.usage.completeness == "complete"
            assert (row.usage.input_tokens, row.usage.output_tokens) == (
                10 * number,
                5 * number,
            )
            assert row.actual_model == "model-snapshot"
            assert row.request_id == (
                request_id if request_id and request_id.strip() else None
            )
            assert row.request_id_missing_reason == (
                None if row.request_id else "provider_not_returned"
            )
            assert row.stop_reason == (
                "success"
                if success
                else "retry_exhausted"
                if number == 3
                else "retry_scheduled"
            )
            assert row.cost.amount is None  # No price supplied, never substitute zero.
        with pytest.raises(ContractError):
            await client.complete("x", call=call)
        assert len(sent) == expected
    finally:
        await client.close()
