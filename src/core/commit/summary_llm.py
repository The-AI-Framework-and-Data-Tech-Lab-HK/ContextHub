"""LLM-based trajectory summarizer for L0/L1 outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI


@dataclass
class LLMTrajectorySummarizer:
    api_key: str
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    temperature: float = 0.0
    last_traces: list[dict[str, Any]] = field(default_factory=list)

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
        return {}

    def summarize(self, steps: list[dict[str, Any]]) -> tuple[str, str]:
        self.last_traces = []
        prompt = (
            "You summarize an agent trajectory into two Chinese summaries.\n"
            "Return JSON only: {'l0': str, 'l1': str}.\n"
            "Requirements:\n"
            "- l0: 100-150 Chinese characters. Include task goal, high-level steps, execution quality/outcome.\n"
            "- l1: 600-800 Chinese characters. Describe major path, what each stage did, key outputs, failures/retries, and final effect.\n"
            "- Be factual and concise. Do not invent details not present in trajectory."
        )
        client = self._client()
        resp = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"trajectory": steps}, ensure_ascii=False)},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = self._extract_json(content)
        l0 = str(data.get("l0") or "").strip()
        l1 = str(data.get("l1") or "").strip()
        self.last_traces.append(
            {
                "call_type": "summary",
                "model": self.model,
                "base_url": self.base_url or "",
                "temperature": self.temperature,
                "raw_response_text": content,
                "parsed_result": {"l0": l0, "l1": l1},
                "error": "",
            }
        )
        if not l0 or not l1:
            raise ValueError("llm summary output missing l0/l1")
        return l0, l1

