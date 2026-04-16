/**
 * ContextHub agent tools for OpenClaw.
 *
 * Eight MVP tools that proxy to the ContextHub Python sidecar.
 * Each tool is registered via api.registerTool() in the plugin entry point.
 */

import type { ContextHubBridge } from "./bridge.js";

type ToolResult = { content: Array<{ type: string; text: string }>; details?: unknown };

function textResult(data: unknown): ToolResult {
  const text = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  return { content: [{ type: "text", text }], details: data };
}

function makeTool(
  bridge: ContextHubBridge,
  name: string,
  description: string,
  parameters: Record<string, unknown>,
) {
  return {
    name,
    description,
    parameters,
    execute: async (
      _toolCallId: string,
      params: Record<string, unknown>,
    ) => {
      try {
        const result = await bridge.dispatchTool(name, params ?? {});
        return textResult(result);
      } catch (err) {
        return textResult({ error: String(err) });
      }
    },
  };
}

/**
 * Create all 8 ContextHub tools bound to a bridge instance.
 */
export function createContextHubTools(bridge: ContextHubBridge) {
  return [
    makeTool(bridge, "ls", "List ContextHub contexts under a URI prefix.", {
      type: "object",
      properties: {
        path: { type: "string", description: "URI prefix (e.g. ctx://)" },
      },
      required: ["path"],
    }),

    makeTool(bridge, "read", "Read a ContextHub context by URI.", {
      type: "object",
      properties: {
        uri: { type: "string", description: "Context URI (e.g. ctx://agent/...)" },
        version: { type: "integer", description: "Specific skill version to read" },
      },
      required: ["uri"],
    }),

    makeTool(bridge, "grep", "Search ContextHub contexts by keyword or semantic query.", {
      type: "object",
      properties: {
        query: { type: "string", description: "Search query" },
        top_k: { type: "integer", description: "Max results to return", default: 5 },
      },
      required: ["query"],
    }),

    makeTool(bridge, "stat", "Get metadata about a ContextHub context.", {
      type: "object",
      properties: {
        uri: { type: "string", description: "Context URI" },
      },
      required: ["uri"],
    }),

    makeTool(
      bridge,
      "contexthub_store",
      "Store a private memory for future recall. Use when the user asks you to remember, save, or note down information.",
      {
        type: "object",
        properties: {
          content: { type: "string", description: "Memory content to store" },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "Optional tags for categorization",
          },
        },
        required: ["content"],
      },
    ),

    makeTool(
      bridge,
      "contexthub_promote",
      "Promote a private memory to team-visible shared knowledge.",
      {
        type: "object",
        properties: {
          uri: { type: "string", description: "URI of the private memory" },
          target_team: { type: "string", description: "Team to promote to" },
        },
        required: ["uri", "target_team"],
      },
    ),

    makeTool(
      bridge,
      "contexthub_skill_publish",
      "Publish a new version of a skill.",
      {
        type: "object",
        properties: {
          skill_uri: { type: "string", description: "Skill URI" },
          content: { type: "string", description: "Skill version content" },
          changelog: { type: "string", description: "Changelog entry" },
          is_breaking: { type: "boolean", description: "Whether this is a breaking change" },
        },
        required: ["skill_uri", "content"],
      },
    ),

    makeTool(
      bridge,
      "contexthub_feedback",
      "Report whether a retrieved ContextHub context was adopted or ignored.",
      {
        type: "object",
        properties: {
          context_uri: { type: "string", description: "Context URI being rated" },
          outcome: {
            type: "string",
            enum: ["adopted", "ignored", "corrected", "irrelevant"],
            description: "Feedback outcome for the context",
          },
          retrieval_id: {
            type: "string",
            description: "SearchResponse.retrieval_id when available",
          },
          metadata: {
            type: "object",
            description: "Optional structured feedback metadata",
          },
        },
        required: ["context_uri", "outcome"],
      },
    ),
  ];
}
