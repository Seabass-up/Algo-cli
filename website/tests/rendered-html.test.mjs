import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const projectRoot = new URL("../", import.meta.url);

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${pathname}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the Algo CLI product home", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>Algo CLI — Verified work\. Local control\.<\/title>/i);
  assert.match(html, /Verified work\./);
  assert.match(html, /Local control\./);
  assert.match(html, /9\/9/);
  assert.match(html, /Public knowledge plane/i);
  assert.match(html, /\/api\/v1\/releases\/stable\.json/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("renders the core support routes", async () => {
  for (const [path, expected] of [
    ["/install", /Start local/],
    ["/docs", /Command field guide/],
    ["/doctor", /Local browser analysis/i],
    ["/benchmarks", /Show the receipts/],
    ["/security", /Trust boundaries/i],
  ]) {
    const response = await render(path);
    assert.equal(response.status, 200, path);
    assert.match(await response.text(), expected, path);
  }
});

test("ships truthful machine-readable release and benchmark contracts", async () => {
  const release = JSON.parse(await readFile(new URL("../public/api/v1/releases/stable.json", import.meta.url), "utf8"));
  const benchmark = JSON.parse(await readFile(new URL("../public/benchmarks/summary.json", import.meta.url), "utf8"));
  const docs = JSON.parse(await readFile(new URL("../public/docs/index.json", import.meta.url), "utf8"));
  assert.equal(release.channel, "release-candidate");
  assert.equal(release.published, false);
  assert.equal(release.package.available, false);
  assert.equal(benchmark.protocol.total_runs, 27);
  assert.equal(benchmark.results[0].objective_passes, 9);
  assert.match(benchmark.limitations, /do not support a universal superiority/i);
  assert.equal(docs.version, "0.14.0");
  await access(new URL("../public/llms.txt", import.meta.url));
  await access(new URL("../public/og.png", import.meta.url));
  await assert.rejects(access(new URL("../app/_sites-preview", projectRoot)));
});
