from __future__ import annotations

import threading

from integrations.entcollabbench.online_audit import ThreadedEnforcementAuditSink


class Record:
    def to_json(self) -> dict:
        return {
            "agent_id": "collaboration_ops_specialist",
            "server": "teams",
            "tool_name": "send_channel_message",
            "mode": "observe",
            "verdict": "allow",
            "action": "allow",
            "allow": True,
            "would_block": False,
            "would_repair": False,
            "forwarded": True,
            "violations": [],
        }


def test_threaded_enforcement_audit_sink_writes_off_path() -> None:
    written: list[dict] = []
    seen = threading.Event()

    async def writer(record: dict) -> None:
        written.append(record)
        seen.set()

    sink = ThreadedEnforcementAuditSink(writer)

    sink(Record())
    assert seen.wait(timeout=2)
    sink.close(drain=True)

    assert written[0]["tool_name"] == "send_channel_message"


def test_threaded_enforcement_audit_sink_failures_do_not_raise() -> None:
    seen = threading.Event()

    async def writer(record: dict) -> None:
        seen.set()
        raise RuntimeError("audit down")

    sink = ThreadedEnforcementAuditSink(writer)

    sink(Record())
    assert seen.wait(timeout=2)
    sink.close(drain=True)
