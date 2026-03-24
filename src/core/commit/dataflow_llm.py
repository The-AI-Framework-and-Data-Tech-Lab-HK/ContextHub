"""LLM-based dataflow extractor for commit graph building."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


@dataclass
class LLMDataflowExtractor:
    """Extract dataflow edges from node IO using an LLM."""

    api_key: str
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    temperature: float = 0.0

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

    def extract(
        self,
        *,
        nodes: list[dict[str, Any]],
        threshold: float,
        top_k_per_dst: int,
    ) -> list[dict[str, Any]]:
        """
        Returns edge suggestions:
        [
          {
            "src_node_id": "...",
            "dst_node_id": "...",
            "confidence": 0.78,
            "evidence_type": "enum_to_command",
            "matched_tokens": ["..."],
            "reason": "..."
          }
        ]
        """
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
            "{'edges':[{'src_node_id':str,'dst_node_id':str,'confidence':float,"
            "'evidence_type':str,'matched_tokens':[str],'reason':str}]}\n"
            f"Only include edges with confidence >= {threshold}. "
            f"For each dst node, keep at most {top_k_per_dst} best edges."
        )

        client = self._client()
        resp = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"nodes": nodes}, ensure_ascii=False)},
            ],
        )
        content = resp.choices[0].message.content or '{"edges":[]}'
        data = self._extract_json(content)
        edges = data.get("edges")
        if not isinstance(edges, list):
            return []
        out: list[dict[str, Any]] = []
        for e in edges:
            if not isinstance(e, dict):
                continue
            out.append(e)
        return out

