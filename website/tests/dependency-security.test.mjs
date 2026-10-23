import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const yaml = createRequire(require.resolve("@eslint/eslintrc"))("js-yaml");

test("non-Linux Rolldown bindings do not inherit Linux libc restrictions", () => {
  const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url), "utf8"));
  for (const [name, entry] of Object.entries(lock.packages)) {
    if (name.startsWith("node_modules/@rolldown/binding-") && !entry.os?.includes("linux")) {
      assert.equal(entry.libc, undefined, `${name} must not be filtered by Linux libc`);
    }
  }
  assert.deepEqual(lock.packages["node_modules/@rolldown/binding-linux-x64-gnu"].libc, ["glibc"]);
  assert.deepEqual(lock.packages["node_modules/@rolldown/binding-linux-x64-musl"].libc, ["musl"]);
});

test("YAML empty merge sources consume the configured work budget", () => {
  const emptySources = Array.from({ length: 20 }, () => "{}").join(",");
  const source = `sources: &sources [${emptySources}]\nitems:\n${"  - <<: *sources\n".repeat(20)}`;
  assert.throws(
    () => yaml.load(source, { schema: yaml.YAML11_SCHEMA, maxTotalMergeKeys: 100 }),
    /maxTotalMergeKeys/,
  );
  const withinBudget = yaml.load(source, { schema: yaml.YAML11_SCHEMA, maxTotalMergeKeys: 1000 });
  assert.deepEqual(withinBudget.items, Array.from({ length: 20 }, () => ({})));
});

test("ordinary YAML inheritance remains available within its budget", () => {
  const source = "base: &base {name: Algo}\nitem:\n  <<: *base\n  state: ready\n";
  assert.deepEqual(yaml.load(source, { maxTotalMergeKeys: 100 }).item, { name: "Algo", state: "ready" });
});

for (const consumer of ["next", "miniflare"]) {
  test(`${consumer} resolves patched native image decoders with working AVIF and SVG support`, async () => {
    const dependencyRequire = createRequire(require.resolve(consumer));
    const sharp = dependencyRequire("sharp");
    const semver = createRequire(dependencyRequire.resolve("sharp"))("semver");
    assert.ok(semver.gte(sharp.versions.sharp, "0.35.5"), `affected sharp ${sharp.versions.sharp}`);
    assert.ok(semver.gte(sharp.versions.heif, "1.23.2"), `affected libheif ${sharp.versions.heif}`);
    assert.ok(semver.gte(sharp.versions.rsvg, "2.63.2"), `affected librsvg ${sharp.versions.rsvg}`);
    const input = await sharp({
      create: { width: 8, height: 6, channels: 3, background: { r: 32, g: 96, b: 160 } },
    }).avif().toBuffer();
    const result = await sharp(input).resize(4, 3).png().toBuffer({ resolveWithObject: true });
    assert.equal(result.info.format, "png");
    assert.equal(result.info.width, 4);
    assert.equal(result.info.height, 3);
    const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="6"><rect width="8" height="6" fill="#2060a0"/></svg>');
    const renderedSvg = await sharp(svg).png().toBuffer({ resolveWithObject: true });
    assert.equal(renderedSvg.info.width, 8);
    assert.equal(renderedSvg.info.height, 6);
  });
}
