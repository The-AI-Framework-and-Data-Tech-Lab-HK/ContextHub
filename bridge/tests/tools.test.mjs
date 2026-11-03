import assert from "node:assert/strict";
import test from "node:test";

import register from "../dist/index.js";

test("OpenClaw registers only prefixed tools and forwards renamed calls to the sidecar", async (t) => {
  const tools = [];
  register({
    pluginConfig: { sidecarUrl: "http://sidecar.test/" },
    registerContextEngine() {},
    registerTool(factory) { tools.push(factory()); },
  });
  assert.deepEqual(tools.map((tool) => tool.name), [
    "contexthub_ls",
    "contexthub_read",
    "contexthub_search",
    "contexthub_stat",
    "contexthub_store",
    "contexthub_promote",
    "contexthub_skill_publish",
    "contexthub_feedback",
  ]);

  const requests = [];
  const response = { marker: "sidecar-result" };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "http://sidecar.test/dispatch");
    assert.equal(options.method, "POST");
    requests.push(JSON.parse(options.body));
    return Response.json(response);
  });
  for (const [name, args] of [
    ["contexthub_ls", { path: "ctx://team/engineering" }],
    ["contexthub_read", { uri: "ctx://team/engineering/resources/orders", version: 2 }],
    ["contexthub_search", { query: "orders", top_k: 3 }],
    ["contexthub_stat", { uri: "ctx://team/engineering/resources/orders" }],
  ]) {
    const tool = tools.find((tool) => tool.name === name);
    const result = await tool.execute("call-1", args);
    assert.deepEqual(requests.at(-1), { name, args });
    assert.deepEqual(result.details, response);
    assert.deepEqual(JSON.parse(result.content[0].text), response);
  }
  assert.equal(requests.length, 4);
});
