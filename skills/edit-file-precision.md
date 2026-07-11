---
name: edit-file-precision
description: Use edit_file (find/replace) instead of write_file for surgical edits to existing files. Faster, fewer tokens, less error-prone.
tags: [algo-cli, edit_file, workflow, file-mutation]
created: 2026-06-09
---

# Edit-File Precision

## Trigger

When the model needs to modify an existing file (not create a new one, not
do a full rewrite), reach for `edit_file` first.

## Steps

1. If you don't know the exact current contents of the file, call `read_file`
   first. This avoids failed matches from stale mental models of the file.
2. Construct `old_string` with 2-5 lines of surrounding context so the match
   is unique. Use whitespace/indentation exactly as it appears in the file.
3. Call `edit_file(path, old_string, new_string)`.
4. If the tool returns "matched N locations", tighten `old_string` (add more
   context) or pass `replace_all=True` only when the rewrite is genuinely
   global.
5. If the tool returns "old_string not found", re-`read_file` and look for
   trailing whitespace, CRLF, or tabs/spaces mismatches, then retry.
6. After a successful edit, optionally `read_file` the changed section to
   verify the result before claiming completion.

## Key Discoveries

- edit_file uses `_atomic_write_text` (write-to-tmp + `os.replace`) so partial
  power loss cannot corrupt the file. It is safer than `sed -i` in shell.
- For files larger than 50k characters, prefer multiple small `edit_file`
  calls over one large `write_file` — each edit returns the affected line
  range, which is much easier to verify than a full rewrite.
- When making the SAME change in many files (e.g. rename an import), loop
  with `edit_file` and check the return string for the line count to confirm
  the match was unique in each file.
- edit_file is registered as a DANGEROUS tool (requires `/auto` to skip
  approval) the same way `write_file` is. Plan the prompt for the user.
- For multi-line replacements, keep `old_string` short but include the
  smallest unique anchor — typically the function signature plus one body
  line is enough.

## Environment

algo-cli >= 0.4 (edit_file introduced 2026-06-09). Works on any platform
where write-to-tmp + os.replace works (Linux, macOS, Windows).
