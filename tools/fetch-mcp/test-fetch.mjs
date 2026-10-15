import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

// 可指定旧版本入口复现故障；正常验收只使用锁文件管理的独立依赖。
const entry = process.env.FETCH_MCP_TEST_ENTRY
  ? resolve(process.env.FETCH_MCP_TEST_ENTRY)
  : fileURLToPath(new URL("node_modules/@yawlabs/fetch-mcp/dist/index.js", import.meta.url));
const packageRequire = createRequire(entry);
const { Client } = await import(pathToFileURL(
  packageRequire.resolve("@modelcontextprotocol/sdk/client/index.js"),
).href);
const { StdioClientTransport } = await import(pathToFileURL(
  packageRequire.resolve("@modelcontextprotocol/sdk/client/stdio.js"),
).href);
const client = new Client({ name: "fetch-regression", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: process.env.FETCH_MCP_TEST_ADAPTER
    ? [resolve(process.env.FETCH_MCP_TEST_ADAPTER), process.execPath, entry]
    : [entry],
  stderr: "pipe",
});
// 不将依赖诊断原文写入终端，避免环境变量或潜在凭据进入验收产物。
transport.stderr?.resume();

before(async () => {
  await client.connect(transport, { timeout: 15000 });
});
after(async () => {
  await client.close();
});

function textContent(result) {
  return (result.content ?? [])
    .filter((item) => item.type === "text")
    .map((item) => item.text)
    .join("\n");
}

test("DNS-pinned public GET returns HTTP status, response headers and body", { timeout: 40000 }, async () => {
  const result = await client.callTool({
    name: "http_get",
    arguments: { url: "https://example.com/", timeout_ms: 15000, max_bytes: 65536 },
  }, undefined, { timeout: 25000 });
  assert.notEqual(result.isError, true, "Public GET must succeed with SSRF protection enabled");
  const text = textContent(result);
  assert.match(text, /^HTTP\/1\.1 200\b/);
  assert.match(text, /content-type: text\/html/i);
  assert.match(text, /--- Body ---[\s\S]*Example Domain/);
});

test("HEAD returns response headers without a body", { timeout: 40000 }, async () => {
  const result = await client.callTool({
    name: "http_head",
    arguments: { url: "https://example.com/", timeout_ms: 15000 },
  }, undefined, { timeout: 25000 });
  assert.notEqual(result.isError, true);
  const text = textContent(result);
  assert.match(text, /^HTTP\/1\.1 200\b/);
  assert.match(text, /content-type: text\/html/i);
  assert.doesNotMatch(text, /--- Body/);
});

test("HTML extraction returns readable content", { timeout: 40000 }, async () => {
  const result = await client.callTool({
    name: "fetch_html_to_text",
    arguments: { url: "https://example.com/", timeout_ms: 15000 },
  }, undefined, { timeout: 25000 });
  assert.notEqual(result.isError, true);
  assert.match(textContent(result), /Example Domain/);
});

test("Loopback requests remain blocked by default", { timeout: 10000 }, async () => {
  for (const url of ["http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/"]) {
    const result = await client.callTool({
      name: "http_get",
      arguments: { url },
    }, undefined, { timeout: 5000 });
    assert.equal(result.isError, true);
    assert.match(textContent(result), /blocked|not allowed|private|loopback/i);
  }
});
