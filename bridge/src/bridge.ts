/**
 * ContextHubBridge: HTTP adapter implementing the OpenClaw ContextEngine interface.
 *
 * All business logic lives in the Python plugin (sidecar). This bridge only
 * does protocol forwarding and type adaptation.
 */

// OpenClaw ContextEngine interface shape (from openclaw/plugin-sdk).
// Using inline types since we don't have the real SDK package available.
export interface ContextEngineInfo {
  kind: string;
  id: string;
  name: string;
}

export interface ContextEngine {
  readonly info: ContextEngineInfo;
  tools: any[];
  ingest(params: any): Promise<any>;
  ingestBatch?(params: any): Promise<any>;
  assemble(params: any): Promise<any>;
  afterTurn(params: any): Promise<void>;
  compact(params: any): Promise<any>;
  dispose(): Promise<void>;
}

export class ContextHubBridge implements ContextEngine {
  private sidecarUrl: string;
  private _info: ContextEngineInfo;
  private _tools: any[] = [];

  constructor(sidecarUrl: string) {
    this.sidecarUrl = sidecarUrl.replace(/\/$/, "");
    // Default info; will be refreshed on first access via fetchInfo()
    this._info = { kind: "context-engine", id: "contexthub", name: "contexthub" };
  }

  get info(): ContextEngineInfo {
    return this._info;
  }

  /** Fetch and cache info from sidecar. Call once after construction. */
  async fetchInfo(): Promise<void> {
    this._info = await this.get("/info");
  }

  /** Fetch and cache tool definitions from sidecar. */
  async fetchTools(): Promise<any[]> {
    this._tools = await this.get("/tools");
    return this._tools;
  }

  get tools(): any[] {
    return this._tools;
  }

  async dispatchTool(name: string, args: Record<string, any>): Promise<any> {
    return this.post("/dispatch", { name, args });
  }

  async ingest(params: any): Promise<any> {
    return this.post("/ingest", params);
  }

  async ingestBatch(params: any): Promise<any> {
    return this.post("/ingest-batch", params);
  }

  async assemble(params: any): Promise<any> {
    return this.post("/assemble", params);
  }

  async afterTurn(params: any): Promise<void> {
    await this.post("/after-turn", params);
  }

  async compact(params: any): Promise<any> {
    const result = await this.post("/compact", params);
    if (!result.compacted) {
      // ContextHub does not own compaction.
      // In a real OpenClaw environment, the runtime would delegate to
      // LegacyContextEngine here. This is a known boundary — not a fake integration.
      // TODO: Wire up tryLegacyCompact() when OpenClaw runtime API is available.
    }
    return result;
  }

  async dispose(): Promise<void> {
    await this.post("/dispose", {});
  }

  // --- HTTP helpers ---

  private async get(path: string): Promise<any> {
    const resp = await fetch(`${this.sidecarUrl}${path}`);
    if (!resp.ok) throw new Error(`Sidecar GET ${path} failed: ${resp.status}`);
    return resp.json();
  }

  private async post(path: string, body: any): Promise<any> {
    const resp = await fetch(`${this.sidecarUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`Sidecar POST ${path} failed: ${resp.status}`);
    return resp.json();
  }
}
