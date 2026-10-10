"use strict";

const { statSync } = require("node:fs");
const path = require("node:path");
const { expand } = require("brace-expansion");
const { globSync, hasMagic } = require("glob");

// These two pinned consumers use only synchronous globbing, not Fast Glob's
// streaming, task-generation or custom-filesystem interfaces.
const supportedOptions = new Set(["cwd", "onlyDirectories"]);
const MAX_PATTERN_CHARS = 32768;
const MAX_PATTERN_COUNT = 256;
const MAX_NESTING = 64;

function validatePattern(pattern) {
  if (typeof pattern !== "string" || pattern.length > MAX_PATTERN_CHARS) {
    throw new TypeError("Glob pattern must be a bounded string");
  }
  let escaped = false;
  let braces = 0;
  let parentheses = 0;
  for (const char of pattern) {
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (char === "{") braces++;
    else if (char === "(") parentheses++;
    else if (char === "}") braces = Math.max(0, braces - 1);
    else if (char === ")") parentheses = Math.max(0, parentheses - 1);
    if (braces + parentheses > MAX_NESTING) throw new Error("Glob pattern nesting budget exceeded");
  }
}

function sync(patterns, options = {}) {
  const list = typeof patterns === "string" ? [patterns] : patterns;
  if (!Array.isArray(list) || list.length > MAX_PATTERN_COUNT) {
    throw new TypeError("Glob patterns must be a bounded string list");
  }
  for (const pattern of list) validatePattern(pattern);
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new TypeError("Glob options must be an object");
  }
  for (const key of Object.keys(options)) {
    if (!supportedOptions.has(key)) throw new Error(`Unsupported bounded-glob option: ${key}`);
  }
  const absolute = list.some((pattern) => path.isAbsolute(pattern.replace(/^!/, "")));
  if (absolute && !list.every((pattern) => path.isAbsolute(pattern.replace(/^!/, "")))) {
    throw new Error("Mixed absolute and relative glob patterns are unsupported");
  }
  const cwd = options.cwd || process.cwd();
  const expanded = [];
  for (const pattern of list) {
    const remaining = MAX_PATTERN_COUNT - expanded.length;
    const alternatives = expand(pattern, {
      max: remaining + 1,
      maxLength: MAX_PATTERN_CHARS * (remaining + 1),
    });
    if (alternatives.length > remaining) throw new Error("Glob brace expansion budget exceeded");
    for (const alternative of alternatives) validatePattern(alternative);
    expanded.push(...alternatives);
  }
  const ignore = expanded.filter((pattern) => pattern.startsWith("!")).map((pattern) => pattern.slice(1));
  const matches = [];
  for (const pattern of expanded.filter((pattern) => !pattern.startsWith("!"))) {
    const parts = pattern.split("/");
    const magicIndex = parts.findIndex((part) => hasMagic(part, { magicalBraces: true }));
    const prefix = parts.slice(0, magicIndex < 0 ? -1 : magicIndex).join("/");
    for (const match of globSync(pattern, { cwd, ignore, absolute, nodir: !options.onlyDirectories, follow: true, nobrace: true })) {
      if (options.onlyDirectories && !statSync(path.resolve(cwd, match)).isDirectory()) continue;
      // Vinext uses lexical path strings as runtime switch keys. Keep an
      // explicit dot/dot-dot prefix even when the engine canonicalizes it.
      const lexical = prefix.split("/").some((part) => part === "." || part === "..")
        ? `${prefix}/${path.relative(path.resolve(cwd, prefix), path.resolve(cwd, match)).replaceAll("\\", "/")}`
        : match;
      matches.push(lexical);
    }
  }
  return [...new Set(matches)];
}

module.exports = { sync, globSync: sync };
