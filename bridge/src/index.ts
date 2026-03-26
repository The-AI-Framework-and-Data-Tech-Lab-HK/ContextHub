/**
 * ContextHub OpenClaw Bridge — entry point.
 *
 * Usage:
 *   import { createContextEngine } from "@contexthub/openclaw-bridge";
 *   const engine = createContextEngine({ sidecarUrl: "http://localhost:9100" });
 *   await engine.fetchInfo();
 */

import { ContextHubBridge, type ContextEngine, type ContextEngineInfo } from "./bridge.js";

export { ContextHubBridge, type ContextEngine, type ContextEngineInfo };

export function createContextEngine(config: {
  sidecarUrl: string;
}): ContextHubBridge {
  return new ContextHubBridge(config.sidecarUrl);
}
