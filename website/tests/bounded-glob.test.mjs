import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import commonjs from "vite-plugin-commonjs";

const require = createRequire(import.meta.url);
const nextRequire = createRequire(require.resolve("@next/eslint-plugin-next"));
const dynamicRequire = createRequire(require.resolve("vite-plugin-dynamic-import"));
const nextGlob = nextRequire("fast-glob");
const dynamicGlob = dynamicRequire("fast-glob");
const { getRootDirs } = nextRequire("./utils/get-root-dirs.js");

async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), "algo-glob-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "src", "pages"), { recursive: true });
  await mkdir(path.join(root, "src", "nested"));
  await writeFile(path.join(root, "src", "a.js"), "module.exports = 1;");
  await writeFile(path.join(root, "src", "b.ts"), "export default 2;");
  await writeFile(path.join(root, "src", ".hidden.js"), "module.exports = 3;");
  await writeFile(path.join(root, "src", "nested", "index.js"), "module.exports = 4;");
  await writeFile(path.join(root, "src", "pages", "index.js"), "export default function Home() { return null; }");
  return root;
}

function commonJsPlugin(root, extensions = [".js", ".ts"]) {
  const plugin = commonjs();
  plugin.configResolved({
    root,
    resolve: { extensions, alias: [] },
    optimizeDeps: { esbuildOptions: {} },
    createResolver: () => async (id, importer) => path.resolve(path.dirname(importer), id),
  });
  return plugin;
}

test("both actual callers resolve the bounded adapter rather than braces", () => {
  for (const resolver of [nextRequire, dynamicRequire]) {
    assert.equal(resolver("fast-glob/package.json").name, "@algo-cli/bounded-glob");
    assert.equal(resolver("fast-glob/package.json").dependencies.glob, "13.0.6");
    assert.equal(resolver("fast-glob/package.json").dependencies["brace-expansion"], "5.0.12");
    const installed = path.relative(fileURLToPath(new URL("../", import.meta.url)), realpathSync(resolver.resolve("fast-glob"))).replaceAll("\\", "/");
    assert.ok(installed.startsWith("node_modules/"), "adapter must be packed under node_modules, not linked into source");
  }
  assert.equal(nextGlob.globSync, dynamicGlob.sync);
});

test("extension braces, index resolution, exclusions and duplicate patterns retain results", async (t) => {
  const cwd = await fixture(t);
  assert.deepEqual(dynamicGlob.sync("src/*.{js,ts}", { cwd }).sort(), ["src/a.js", "src/b.ts"]);
  assert.deepEqual(dynamicGlob.sync(["src/**/*.js", "!src/pages/**"], { cwd }).sort(), ["src/a.js", "src/nested/index.js"]);
  assert.deepEqual(dynamicGlob.sync(["src/a.js", "src/a.js"], { cwd }), ["src/a.js"]);
  assert.deepEqual(dynamicGlob.sync("src/missing/*.js", { cwd }), []);
  assert.deepEqual(dynamicGlob.sync("src/.hidden.js", { cwd }), ["src/.hidden.js"]);
  assert.deepEqual(dynamicGlob.sync("src", { cwd }), []);
  assert.deepEqual(nextGlob.globSync("src", { cwd, onlyDirectories: true }), ["src"]);
});

test("Next root discovery preserves default, absolute and normalized Windows-style roots", async (t) => {
  const cwd = await fixture(t);
  const src = path.join(cwd, "src");
  assert.deepEqual(getRootDirs({ cwd, settings: {} }), [cwd]);
  assert.deepEqual(getRootDirs({ cwd, settings: { next: { rootDir: src } } }), [src]);
  assert.deepEqual(getRootDirs({ cwd, settings: { next: { rootDir: src.replaceAll("/", "\\") } } }), [src]);
  assert.deepEqual(getRootDirs({ cwd, settings: { next: { rootDir: [src, path.join(cwd, "missing")] } } }), [src]);
});

test("directory symlinks remain discoverable where supported", async (t) => {
  const cwd = await fixture(t);
  try {
    await symlink(path.join(cwd, "src"), path.join(cwd, "alias"), "junction");
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) return t.skip("OS does not grant symlink creation");
    throw error;
  }
  assert.deepEqual(nextGlob.globSync("alias", { cwd, onlyDirectories: true }), ["alias"]);
  assert.deepEqual(dynamicGlob.sync("alias/*.{js,ts}", { cwd }).sort(), ["alias/a.js", "alias/b.ts"]);
});

test("pathological nesting is rejected before globbing for both entry points", () => {
  for (const glob of [nextGlob.globSync, dynamicGlob.sync]) {
    for (const pattern of ["{".repeat(10000) + "a,b" + "}".repeat(10000), "(".repeat(10000) + "a" + ")".repeat(10000), "[" + "+(".repeat(6000) + "a" + ")".repeat(6000), "{)".repeat(6000) + "a,b" + "}".repeat(6000)]) {
      assert.throws(() => glob(pattern), /nesting budget exceeded/);
    }
    assert.throws(() => glob("a".repeat(32769)), /bounded string/);
    assert.throws(() => glob(Array(257).fill("a")), /bounded string list/);
    assert.throws(() => glob([null]), /bounded string/);
    assert.throws(() => glob("*", { fs: {} }), /Unsupported bounded-glob option/);
  }
});

test("Vinext's CommonJS transform retains static and dynamic require behavior", async (t) => {
  const root = await fixture(t);
  const plugin = commonJsPlugin(root);
  const id = path.join(root, "entry.js");
  const staticResult = await plugin.transform('const value = require("./src/a.js"); module.exports = value;', id);
  assert.match(staticResult.code, /import .*src\/a\.js/);
  const dynamicResult = await plugin.transform('const name = "a"; const value = require("./src/" + name); module.exports = value;', id);
  assert.match(dynamicResult.code, /src\/a\.js/);
  assert.match(dynamicResult.code, /src\/b\.ts/);
  assert.match(dynamicResult.code, /src\/nested\/index\.js/);
  assert.match(dynamicResult.code, /switch\s*\(path\)/);
});

test("Next's internal-link lint rule still rejects anchors and accepts Link", async (t) => {
  const cwd = await fixture(t);
  const { ESLint } = require("eslint");
  const nextPlugin = require("@next/eslint-plugin-next");
  const eslint = new ESLint({
    cwd,
    overrideConfigFile: true,
    overrideConfig: [{
      files: ["**/*.js"],
      languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
      plugins: { "@next/next": nextPlugin },
      settings: { next: { rootDir: path.join(cwd, "src") } },
      rules: { "@next/next/no-html-link-for-pages": "error" },
    }],
  });
  const [invalid] = await eslint.lintText('export default function App() { return <a href="/">Home</a>; }', { filePath: path.join(cwd, "component.js") });
  assert.equal(invalid.messages.filter((row) => row.ruleId === "@next/next/no-html-link-for-pages").length, 1);
  const [valid] = await eslint.lintText('export default function App() { return <Link href="/">Home</Link>; }', { filePath: path.join(cwd, "component.js") });
  assert.equal(valid.errorCount, 0);
});

test("wildcard root discovery includes symlink applications and numeric brace ranges", async (t) => {
  const root = await fixture(t);
  await mkdir(path.join(root, "apps"));
  try {
    await symlink(path.join(root, "src"), path.join(root, "apps", "web"), "junction");
    const roots = getRootDirs({ cwd: root, settings: { next: { rootDir: path.join(root, "apps", "*") } } });
    assert.deepEqual(roots, [path.join(root, "apps", "web")]);
  } catch (error) {
    if (!["EPERM", "EACCES"].includes(error.code)) throw error;
    t.diagnostic("OS did not grant symlink creation; numeric range check still runs");
  }
  await mkdir(path.join(root, "packages"));
  for (let i = 1; i <= 12; i++) await mkdir(path.join(root, "packages", `app${i}`));
  const numeric = getRootDirs({ cwd: root, settings: { next: { rootDir: path.join(root, "packages", "app{1..12}") } } });
  assert.equal(numeric.length, 12);
  assert.ok(numeric.includes(path.join(root, "packages", "app12")));
});

test("Vinext retains lexical dot-dot dynamic require switch keys", async (t) => {
  const root = await fixture(t);
  await writeFile(path.join(root, "own.js"), "module.exports = 41;");
  const plugin = commonJsPlugin(root, [".js"]);
  const result = await plugin.transform('const name = "own"; module.exports = require("./src/../" + name);', path.join(root, "entry.js"));
  assert.match(result.code, /case '\.\/src\/\.\.\/own':/);
  assert.match(result.code, /case '\.\/src\/\.\.\/own\.js':/);
  const entry = path.join(root, "entry.mjs");
  await writeFile(entry, result.code);
  const loaded = await import(pathToFileURL(entry).href);
  assert.equal(loaded.default, 41);
});

test("oversized expansion fails explicitly rather than dropping roots", async (t) => {
  const root = await fixture(t);
  for (const glob of [nextGlob.globSync, dynamicGlob.sync]) {
    for (const pattern of ["app{1..1000000000}", "{a,b}".repeat(16)]) {
      assert.throws(() => glob(pattern), /brace expansion budget exceeded/);
    }
    assert.throws(() => glob(["app{1..200}", "other{1..200}"]), /brace expansion budget exceeded/);
  }
  assert.throws(() => getRootDirs({ cwd: root, settings: { next: { rootDir: path.join(root, "app{1..1000000000}") } } }), /brace expansion budget exceeded/);
});

test("malformed class prefixes cannot stall either actual caller", async (t) => {
  const root = await fixture(t);
  const malformed = "[" + "+(".repeat(6000) + "a" + ")".repeat(6000);
  assert.throws(() => getRootDirs({ cwd: root, settings: { next: { rootDir: malformed } } }), /nesting budget exceeded/);
  const code = `const name = "a"; module.exports = require(${JSON.stringify(`./${malformed}/`)} + name);`;
  await assert.rejects(commonJsPlugin(root).transform(code, path.join(root, "entry.js")), /nesting budget exceeded/);
});

test("numeric and wildcard symlink roots preserve actual Next lint coverage", async (t) => {
  const root = await fixture(t);
  const { ESLint } = require("eslint");
  const nextPlugin = require("@next/eslint-plugin-next");
  async function violations(rootDir, href) {
    const eslint = new ESLint({
      cwd: root,
      overrideConfigFile: true,
      overrideConfig: [{
        files: ["**/*.js"],
        languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
        plugins: { "@next/next": nextPlugin },
        settings: { next: { rootDir } },
        rules: { "@next/next/no-html-link-for-pages": "error" },
      }],
    });
    const [result] = await eslint.lintText(`export default function App() { return <a href="${href}">Home</a>; }`, { filePath: path.join(root, "component.js") });
    return result.messages.filter((row) => row.ruleId === "@next/next/no-html-link-for-pages").length;
  }
  for (let i = 1; i <= 12; i++) await mkdir(path.join(root, "packages", `app${i}`), { recursive: true });
  await mkdir(path.join(root, "packages", "app12", "pages"));
  await writeFile(path.join(root, "packages", "app12", "pages", "page12.js"), "export default function Page() { return null; }");
  assert.equal(await violations(path.join(root, "packages", "app{1..12}"), "/page12"), 1);
  await mkdir(path.join(root, "apps"));
  try {
    await symlink(path.join(root, "src"), path.join(root, "apps", "web"), "junction");
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) return t.diagnostic("Symlink lint branch unavailable on this OS");
    throw error;
  }
  assert.equal(await violations(path.join(root, "apps", "*"), "/"), 1);
});
