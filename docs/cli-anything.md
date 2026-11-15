# Native CLI-Anything Integration

Algo discovers executable `cli-anything-*` adapters on the host PATH without
importing their code, running CLI-Hub, reading a remote catalog or installing
packages. Executable discovery is not backend or version qualification.

## Agent Tools

- `cli_anything_discover(query, offset, limit)` lists installed adapters and
  effective execution restrictions, with bounded scanning and pagination.
- `cli_anything_run(adapter, arguments, install_missing, update_first, timeout)`
  invokes an adapter with a string argument array, not shell source. Empty
  arguments request `--help`, never an interactive REPL. A missing adapter is
  installed through CLI-Hub by default; `install_missing=false` disables that.
  `update_first=true` explicitly updates before invoking; existing custom
  adapters are not automatically updated by default.
- `cli_anything_manage(adapter, operation, timeout)` installs or updates a
  named adapter through the installed CLI-Hub. No arbitrary package URL,
  executable, interpreter, install flags, uninstall or bulk matrix is accepted.
  Before changing packages, a bounded CLI-Hub query must identify exactly one
  matching harness entry with the expected `cli-anything-<name>` executable;
  public/non-harness registry entries are not installed through this tool.

Both execution tools are code execution, may mutate global packages or external
application state, and may contact networks. They require the runtime's scoped
capabilities and approval. Safe mode blocks them. With Continuum selected they
remain disabled, including help, installs and updates, until a separate contained
execution boundary is qualified. They do not disable or fall back from memory.
Native discovery remains available in that configuration and reports the block.

The host owns cwd, configuration and cancellation; the model cannot override
them. Calls share one bounded deadline across install/update/run, use bounded
stream capture and process-group termination, and report partial/unknown effects.
A zero exit means only the command exited successfully: inspect the resulting
application state or artifact through an independently admitted tool. Neither
discovery nor execution counts as a successful verification of workspace edits.
PATH resolution is not an executable identity pin or a sandbox. Installed
adapters and their real backends remain separate dependencies. CLI-Hub is not
added to Algo's required Python dependencies; its absence is an actionable error.
For a separately installed CLI-Hub environment, set the host-only
`cli_anything_bin_dir` configuration field to its absolute `bin` directory
(`Scripts` on Windows). Discovery, executable resolution and child PATH all
use that same directory. This avoids treating successful installation into a
different environment as a usable adapter. CLI-Hub analytics are disabled in
these child processes. No model-callable path or environment override is added.

## Test Plan

Before implementation: test registration/schema and action-policy coverage;
bounded offline discovery/pagination, PATH precedence and missing backends;
invalid names/argv/timeouts and no shell interpolation; safe/protected-memory
denials before resolution or subprocesses; ordinary runtime grant denial;
install-on-missing, explicit update, unchanged installed adapters, failed package
changes and re-resolution; deadline/output limits and cancellation; mutation
tracking without false verification. Use fake adapter/manager processes for
package-change tests, not real installations. Separately probe an existing
installed adapter's help and real native discovery through the installed runtime.
Never report fake package changes as a live install or application workflow.

## Qualification Boundaries

The regression suite covers native registry/schema/approval integration, a
scripted-provider agent turn, typed argv, bounded output, cancellation and package
management ordering. Installation/update, catalog filtering and re-resolution
tests use local fixtures; they do not prove a real package upgrade succeeded.
Neither an adapter executable nor its help output qualifies the actual application
backend or a document/render/export workflow.

CLI-Hub source selection is not an immutable revision pin, package transaction
or sandbox; independent package-manager processes are outside Algo's action
leases. Qualified containment, physical platform execution and end-to-end
application workflows are separate acceptance gates. Local review qualification
must be executed against the public source, without copying private installation
reports or claiming external blockers passed.
