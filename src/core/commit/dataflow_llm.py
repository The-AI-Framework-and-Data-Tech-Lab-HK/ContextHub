"""LLM-based extractor for dataflow/reasoning edges."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from core.commit.llm_runtime import chat_completion_with_retry, provider_key


@dataclass
class LLMDataflowExtractor:
    """Extract dataflow/reasoning edges from node IO using an LLM."""

    api_key: str
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    temperature: float = 0.0
    _thread_local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    @property
    def last_traces(self) -> list[dict[str, Any]]:
        value = getattr(self._thread_local, "last_traces", None)
        if isinstance(value, list):
            return value
        value = []
        self._thread_local.last_traces = value
        return value

    @last_traces.setter
    def last_traces(self, value: list[dict[str, Any]]) -> None:
        self._thread_local.last_traces = list(value)

    def _client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    def _extract_json(self, text: str) -> dict[str, Any]:
        raw = text.strip()
        try:
            return json.loads(raw)
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        return {"edges": []}

    def clear_traces(self) -> None:
        self.last_traces = []

    def _record_trace(
        self,
        *,
        call_type: str,
        threshold: float,
        top_k_per_dst: int,
        raw_response_text: str,
        parsed_result: Any,
        error: str | None = None,
        retry_count: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        self.last_traces.append(
            {
                "call_type": call_type,
                "model": self.model,
                "base_url": self.base_url or "",
                "temperature": self.temperature,
                "threshold": threshold,
                "top_k_per_dst": top_k_per_dst,
                "raw_response_text": raw_response_text,
                "parsed_result": parsed_result,
                "error": error or "",
                "retry_count": int(retry_count),
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
            }
        )

    def _extract_dataflow(
        self,
        *,
        nodes: list[dict[str, Any]],
        threshold: float,
        top_k_per_dst: int,
    ) -> list[dict[str, Any]]:
        prompt = (
            "You are extracting dataflow dependencies between action nodes.\n"
            "A -> B means some OUTPUT of A is consumed by INPUT of B.\n"
            "Important constraints:\n"
            "1) If a value appears in the same node's tool_args and tool_output, treat it as echoed input; "
            "do NOT use it as output evidence for downstream dependencies.\n"
            "2) Partial inclusion counts as dependency: if output token/value appears as a substring or component "
            "inside later tool_args (especially command text), it IS a valid dependency.\n"
            "3) Source-side evidence must come from src.effective_tool_output only. "
            "Do NOT use src.tool_args as output evidence.\n"
            "Return JSON only with schema:\n"
            "{'dataflow_edges':[{'src_node_id':str,'dst_node_id':str,'confidence':float,"
            "'evidence_type':str,'matched_tokens':[str],'reason':str}]}\n"
            f"Only include edges with confidence >= {threshold}. "
            f"For each dst node, keep at most {top_k_per_dst} best edges."
        )

        client = self._client()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"nodes": nodes}, ensure_ascii=False)},
        ]
        resp, retries = chat_completion_with_retry(
            client=client,
            provider_id=provider_key(base_url=self.base_url, model=self.model),
            model=self.model,
            temperature=self.temperature,
            messages=messages,
        )
        content = resp.choices[0].message.content or '{"dataflow_edges":[]}'
        data = self._extract_json(content)
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        # Backward compatibility if model returns old {'edges': [...]} format.
        if isinstance(data.get("edges"), list):
            edges = [e for e in data.get("edges", []) if isinstance(e, dict)]
            self._record_trace(
                call_type="dataflow",
                threshold=threshold,
                top_k_per_dst=top_k_per_dst,
                raw_response_text=content,
                parsed_result={"dataflow_edges": edges},
                retry_count=retries,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
            return edges
        dataflow_raw = data.get("dataflow_edges")
        edges = [e for e in (dataflow_raw or []) if isinstance(e, dict)] if isinstance(dataflow_raw, list) else []
        self._record_trace(
            call_type="dataflow",
            threshold=threshold,
            top_k_per_dst=top_k_per_dst,
            raw_response_text=content,
            parsed_result={"dataflow_edges": edges},
            retry_count=retries,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        return edges

    def _extract_reasoning(
        self,
        *,
        nodes: list[dict[str, Any]],
        threshold: float,
        top_k_per_dst: int,
    ) -> list[dict[str, Any]]:
        prompt = (
            "You are extracting reasoning dependencies between action nodes.\n"
            "A -> B reasoning means B.thinking references execution evidence from A.\n"
            "Important constraints:\n"
            "1) A must occur before B.\n"
            "2) Prefer evidence from src.effective_tool_output.\n"
            "3) Do NOT use src.tool_args as source output evidence.\n"
            "Return JSON only with schema:\n"
            "{'reasoning_edges':[{'src_node_id':str,'dst_node_id':str,'confidence':float,"
            "'reason_summary':str,'matched_evidence':[str]}]}\n"
            f"Only include edges with confidence >= {threshold}. "
            f"For each dst node, keep at most {top_k_per_dst} best edges."
        )
        client = self._client()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"nodes": nodes}, ensure_ascii=False)},
        ]
        resp, retries = chat_completion_with_retry(
            client=client,
            provider_id=provider_key(base_url=self.base_url, model=self.model),
            model=self.model,
            temperature=self.temperature,
            messages=messages,
        )
        content = resp.choices[0].message.content or '{"reasoning_edges":[]}'
        data = self._extract_json(content)
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        reasoning_raw = data.get("reasoning_edges")
        edges = [e for e in (reasoning_raw or []) if isinstance(e, dict)] if isinstance(reasoning_raw, list) else []
        self._record_trace(
            call_type="reasoning",
            threshold=threshold,
            top_k_per_dst=top_k_per_dst,
            raw_response_text=content,
            parsed_result={"reasoning_edges": edges},
            retry_count=retries,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        return edges

    def extract(
        self,
        *,
        nodes: list[dict[str, Any]],
        threshold: float,
        top_k_per_dst: int,
        reasoning_threshold: float = 0.55,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Run two independent LLM calls:
        - dataflow extraction
        - reasoning extraction
        """
        self.clear_traces()
        dataflow_edges: list[dict[str, Any]] = []
        reasoning_edges: list[dict[str, Any]] = []
        try:
            dataflow_edges = self._extract_dataflow(
                nodes=nodes, threshold=threshold, top_k_per_dst=top_k_per_dst
            )
        except Exception as exc:
            self._record_trace(
                call_type="dataflow",
                threshold=threshold,
                top_k_per_dst=top_k_per_dst,
                raw_response_text="",
                parsed_result={},
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            reasoning_edges = self._extract_reasoning(
                nodes=nodes, threshold=reasoning_threshold, top_k_per_dst=top_k_per_dst
            )
        except Exception as exc:
            self._record_trace(
                call_type="reasoning",
                threshold=reasoning_threshold,
                top_k_per_dst=top_k_per_dst,
                raw_response_text="",
                parsed_result={},
                error=f"{type(exc).__name__}: {exc}",
            )
        return {"dataflow_edges": dataflow_edges, "reasoning_edges": reasoning_edges}

