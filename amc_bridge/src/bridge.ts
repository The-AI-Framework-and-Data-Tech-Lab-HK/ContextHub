type ContextEngineInfo = {
  id: string;
  name: string;
  ownsCompaction?: boolean;
};

type IngestParams = {
  sessionId: string;
  sessionKey?: string;
  message: unknown;
  isHeartbeat?: boolean;
};

type AssembleParams = {
  sessionId: string;
  sessionKey?: string;
  messages: unknown[];
  tokenBudget?: number;
};

type CompactParams = {
  sessionId: string;
  sessionKey?: string;
  sessionFile: string;
  tokenBudget?: number;
  force?: boolean;
};

type IngestResult = { ingested: boolean };
type AssembleResult = { messages: unknown[]; estimatedTokens: number; systemPromptAddition?: string };

export class AMCBridge {
  private sidecarUrl: string;

  constructor(sidecarUrl: string) {
    this.sidecarUrl = sidecarUrl.replace(/\/$/, "");
  }

  get info(): ContextEngineInfo {
    return {
      id: "amc",
      name: "AMC v0",
      ownsCompaction: false,
    };
  }

  async ingest(params: IngestParams): Promise<IngestResult> {
    return this.post("/ingest", params);
  }

  async assemble(params: AssembleParams): Promise<AssembleResult> {
    return this.post("/assemble", params);
  }

  // v0 uses OpenClaw built-in compaction behavior.
  async compact(params: CompactParams): Promise<unknown> {
    try {
      const sdk: { delegateCompactionToRuntime: (p: CompactParams) => Promise<unknown> } =
        await import("openclaw/plugin-sdk");
      return await sdk.delegateCompactionToRuntime(params);
    } catch {
      return { ok: false, compacted: false, reason: "runtime delegation unavailable" };
    }
  }

  async dispose(): Promise<void> {
    return;
  }

  private async post(path: string, body: unknown): Promise<any> {
    const response = await fetch(`${this.sidecarUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`AMC sidecar POST ${path} failed: ${response.status} ${text}`);
    }
    return response.json();
  }
}

