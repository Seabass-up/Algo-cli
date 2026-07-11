# Lessons from the Algo CLI Rebrand (June 2026)

## What Went Well

- **Phased approach was extremely effective.** Splitting the work into Structural Rename → Migration/Default Flip → Branding Sweep allowed each change to be reviewed, tested, and committed cleanly with minimal risk to users.
- **Dual-support shim from the beginning** made the entire migration safe. Being able to read both old and new env vars + config locations during the transition removed almost all user pain.
- **Automatic migration with explicit backup** + sentinel file was the right design. Users get the benefit of the new default without manual steps, while still having a safety net.
- **Keeping the legacy command name registered** for one release was appreciated in the design (even if not yet widely used).

## What We Learned

- Even with careful planning, historical documentation (CLAUDE.md, AGENTS.md, CHANGELOG) requires significant cleanup effort after a rebrand. Plan time for this.
- Internal function names that talk to the Ollama backend (e.g. `active_ollama_client`) should **not** be renamed. Only the product-facing identity changes.
- When doing a full product rename, the harness source root identifiers and cache tags also need updating so RAG continues to work correctly.
- Migration messages need to be very explicit about what changed and where the backup lives. Users in complex multi-agent setups appreciate this clarity.

## Recommendations for Future Rebrands

1. Always implement dual-read support *before* changing any defaults.
2. Create a clear "Migration" section in the changelog early.
3. Generate ready-to-use wiki/memory entries for power users who maintain large personal knowledge bases.
4. Test the legacy shim path explicitly during final smoke testing.

## Personal Takeaway

A well-executed rebrand (even a significant one) can be done without breaking existing users when you prioritize:
- Backward compatibility during transition
- Clear communication
- Automatic (but safe) migration paths

This rebrand significantly improved the product's clarity and positioning while maintaining continuity for existing power users.

**Date:** 2026-06
**Related Commits:** 580c812, dbcdb64, 850fff9