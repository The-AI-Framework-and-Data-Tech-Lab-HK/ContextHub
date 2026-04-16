import { AMCBridge } from "./bridge.js";

const DEFAULT_SIDECAR_URL = "http://localhost:9200";

// Loaded by OpenClaw plugin runtime.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export default function register(api: any): void {
  const sidecarUrl: string = api.pluginConfig?.sidecarUrl ?? DEFAULT_SIDECAR_URL;
  api.registerContextEngine("amc", () => new AMCBridge(sidecarUrl));
}

