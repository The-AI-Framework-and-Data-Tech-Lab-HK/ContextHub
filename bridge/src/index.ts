/**
 * ContextHub OpenClaw Plugin entry point.
 *
 * Loaded by OpenClaw's plugin system. Registers:
 *   1. A ContextEngine ("contexthub") — lifecycle hooks for auto-recall / auto-capture.
 *   2. Eight contexthub_ tools — ls, read, search, stat, store, promote,
 *      skill_publish, and feedback.
 */

import { ContextHubBridge } from "./bridge.js";
import { createContextHubTools } from "./tools.js";

const DEFAULT_SIDECAR_URL = "http://localhost:9100";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export default function register(api: any): void {
  const sidecarUrl: string = api.pluginConfig?.sidecarUrl ?? DEFAULT_SIDECAR_URL;

  api.registerContextEngine("contexthub", () => {
    return new ContextHubBridge(sidecarUrl);
  });

  const lazyBridge = new ContextHubBridge(sidecarUrl);
  for (const tool of createContextHubTools(lazyBridge)) {
    api.registerTool(() => tool);
  }
}

export { ContextHubBridge } from "./bridge.js";
export { createContextHubTools } from "./tools.js";
