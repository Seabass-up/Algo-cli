# Bounded Synchronous Glob Adapter

This local package replaces Fast Glob only for the pinned Next lint utility and
Vinext dynamic-import caller. It is not a general Fast Glob reimplementation.
The two supported exports are `sync` and `globSync`; supported options are `cwd`
and `onlyDirectories`. Unknown options and mixed absolute/relative arrays fail
explicitly rather than silently changing filesystem semantics.

The engine is exact-pinned `glob` 13.0.6 with patched `brace-expansion` 5.0.12,
not the affected recursive `braces` dependency. Preserve absolute paths,
directory symlink roots and lexical dot/dot-dot prefixes used as CommonJS runtime
switch keys. Pattern length, count, nesting and expanded alternative count are
checked before filesystem discovery. Reject oversized expansion explicitly;
never truncate it and silently drop lint roots. Ordinary extension braces,
numeric ranges, exclusions and CommonJS index discovery remain enabled.

An initial Tinyglobby adapter was rejected after executable review reproduced
an unmatched-class nesting bypass, path normalization changing switch keys,
missing wildcard symlink roots and incorrect multi-digit numeric ranges. The
regressions exercise the actual Next lint and Vinext transform callers, not just
the adapter. This limited interface does not claim general Fast Glob compatibility
or a bound on all build-system filesystem traversal.

The direct file dependency plus `$fast-glob` override supplies one reviewed
implementation to both consumers. Qualify every dependency upgrade against
`tests/bounded-glob.test.mjs`, the complete website suite, lint, clean `npm ci`
and full audit. Inspect the lock graph for new Fast Glob consumers or a returning
`braces` path. Do not suppress advisories, omit development dependencies, remove
Next lint rules or downgrade the framework to obtain a green audit.

Engine documentation: https://github.com/isaacs/node-glob
