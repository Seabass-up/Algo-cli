import { PageFrame } from "../site-chrome";

export const metadata = { title: "Docs" };

const groups = [
  { title: "Work", rows: [["/agent TASK", "Run a bounded Agent Blocks pipeline"], ["/agent team TASK", "Fan out read-only specialists, then integrate once"], ["/route TASK", "Preview task classification and advisory budget"], ["/verify on", "Enable claim-grounding verification"]] },
  { title: "Context", rows: [["/remember FACT", "Save an explicit durable memory"], ["/memory-auto status", "Inspect bounded automatic capture"], ["/code-rag on", "Opt in to cwd source indexing"], ["/harness external on", "Opt in to supported external agent stores"]] },
  { title: "Runtime", rows: [["/status", "Inspect model, context, and active features"], ["/doctor", "Run a side-effect-free readiness report"], ["/kernel check", "Validate kernel contracts and wiring"], ["/safe on", "Keep destructive shell patterns blocked"]] },
];

export default function DocsPage() {
  return <PageFrame eyebrow="DOCS / v0.16.0" title="Operate the runtime deliberately." intro="A compact field guide to the controls that matter most. Every feature is designed to reveal its scope, authority, and verification state.">
    <section className="content-wrap docs-layout">
      <aside className="docs-index"><span>ON THIS PAGE</span><a href="#commands">Command field guide</a><a href="#agents">Agent ethos</a><a href="#privacy">Context and privacy</a><a href="#machine-readable">Machine interfaces</a></aside>
      <div className="docs-main">
        <section id="commands"><h2>Command field guide</h2><p>Type <code>/</code> inside Algo CLI for inline completion. These are the highest-leverage controls.</p><div className="command-groups">{groups.map((group) => <article key={group.title}><h3>{group.title}</h3>{group.rows.map(([command, description]) => <div className="command-row" key={command}><code>{command}</code><span>{description}</span></div>)}</article>)}</div></section>
        <section id="agents"><h2>Agent ethos</h2><p>Algo CLI treats delegation as an algorithm, not a personality trick. Specialists receive fresh read-only contexts. Their evidence joins in deterministic role order. A single integration pipeline owns writes, approvals, tests, and the final delta.</p><div className="flow-line"><span>CLASSIFY</span><i>→</i><span>FAN OUT</span><i>→</i><span>JOIN EVIDENCE</span><i>→</i><span>WRITE ONCE</span><i>→</i><span>VERIFY</span></div></section>
        <section id="privacy"><h2>Context and privacy</h2><p>The packaged public corpus is enabled by default. External agent stores, source-code RAG, index-compute-lab, and skill run history require explicit opt-in. Cloud inference may receive the context you enable; review those sources before sending them.</p><a className="text-link" href="https://github.com/Seabass-up/Algo-cli/blob/main/docs/privacy-and-context.md">Read the full privacy contract ↗</a></section>
        <section id="machine-readable"><h2>Machine-readable interfaces</h2><p>These resources are deliberately lean so Algo CLI and other agents can inspect current public information without scraping the visual site.</p><div className="machine-grid"><a href="/llms.txt"><code>/llms.txt</code><span>Canonical agent map</span></a><a href="/docs/index.json"><code>/docs/index.json</code><span>Versioned document catalog</span></a><a href="/api/v1/releases/stable.json"><code>/api/v1/releases/stable.json</code><span>Release state and compatibility</span></a><a href="/benchmarks/summary.json"><code>/benchmarks/summary.json</code><span>Scoped benchmark result</span></a></div></section>
      </div>
    </section>
  </PageFrame>;
}
