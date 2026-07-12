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
  assert.match(html, /pipx install algo-cli-runtime/i);
  assert.match(html, /v0\.14\.0 stable/i);
  assert.match(html, /\/api\/v1\/releases\/stable\.json/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("redirects the www hostname to the canonical origin", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-www`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("https://www.algo-cli.com/docs?source=test"),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
  assert.equal(response.status, 308);
  assert.equal(response.headers.get("location"), "https://algo-cli.com/docs?source=test");
});

test("renders the core support routes", async () => {
  for (const [path, expected] of [
    ["/install", /Start local/],
    ["/docs", /Command field guide/],
    ["/doctor", /Local browser analysis/i],
    ["/benchmarks", /Benchmark the harness\. Not the hype\./],
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
  const discovery = JSON.parse(await readFile(new URL("../public/.well-known/algo-cli.json", import.meta.url), "utf8"));
  assert.equal(release.channel, "stable");
  assert.equal(release.published, true);
  assert.equal(release.package.available, true);
  assert.equal(release.package.name, "algo-cli-runtime");
  assert.equal(release.package.index, "https://pypi.org/project/algo-cli-runtime/");
  assert.equal(release.source_revision, "be25ea08fd0d390d0f21fe8f0646582380ef0a79");
  assert.equal(release.source.available, true);
  assert.equal(release.security_advisory.active, true);
  assert.equal(benchmark.schema_version, 2);
  assert.equal(benchmark.protocol.total_runs, 99);
  assert.equal(benchmark.protocol.measured_harnesses, 11);
  assert.match(benchmark.source_revision, /^[0-9a-f]{40}$/);
  assert.equal(benchmark.runner_path, "benchmarks/competitors");
  assert.equal(benchmark.results.find((row) => row.id === "algo-cli").clean_runs, 9);
  assert.equal(benchmark.results.find((row) => row.id === "algo-cli").rank, 3);
  assert.match(benchmark.limitations, /do not support a universal superiority/i);
  assert.match(benchmark.limitations, /retained locally rather than published/i);
  assert.equal(docs.version, "0.14.0");
  assert.equal(discovery.schema_version, 1);
  assert.equal(discovery.canonical_origin, "https://algo-cli.com");
  assert.equal(discovery.release_channel, "stable");
  assert.equal(discovery.privacy.core_runtime_requires_site, false);
  await access(new URL("../public/llms.txt", import.meta.url));
  await access(new URL("../public/robots.txt", import.meta.url));
  await access(new URL("../public/sitemap.xml", import.meta.url));
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../public/benchmarks/results.csv", import.meta.url));
  await assert.rejects(access(new URL("../app/_sites-preview", projectRoot)));
});
