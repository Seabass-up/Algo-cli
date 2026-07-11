import Link from "next/link";
import { CopyInstall } from "./copy-install";
import { Footer, Header, Pill, SectionHeading } from "./site-chrome";

const pillars = [
  { index: "01", name: "Act", accent: "cyan", text: "Inspect files, edit code, run commands, and work through explicit tool contracts." },
  { index: "02", name: "Remember", accent: "lime", text: "Carry durable context, lessons, and reusable skills across local sessions." },
  { index: "03", name: "Route", accent: "violet", text: "Keep ordinary work direct. Send larger tasks through bounded agent pipelines." },
  { index: "04", name: "Verify", accent: "cyan", text: "Ground completion in tests, file evidence, Git state, and fail-closed policies." },
];

export default function Home() {
  return (
    <main className="site-shell">
      <Header />

      <section className="hero grid-surface" aria-labelledby="hero-title">
        <div className="hero-copy">
          <div className="eyebrow"><span /> Local-first agent runtime</div>
          <h1 id="hero-title">Verified work.<br /><span>Local control.</span></h1>
          <p className="hero-lede">
            Algo CLI combines direct tool use, durable context, routed agents, and explicit verification—
            without making a website or hosted account part of the runtime.
          </p>
          <div className="hero-actions">
            <Link className="button primary" href="/install">Install Algo CLI <b>↗</b></Link>
            <Link className="button secondary" href="/docs">Read the docs <b>→</b></Link>
          </div>
          <div className="hero-meta">
            <Pill tone="lime">v0.14.0 stable</Pill>
            <span>Python 3.10+</span>
            <span>MIT licensed</span>
          </div>
        </div>

        <div className="terminal-zone" aria-label="Example verified Algo CLI run">
          <div className="zone-label">LIVE / LOCAL</div>
          <div className="terminal-card">
            <div className="terminal-head">
              <div className="terminal-lights"><i /><i /><i /></div>
              <span>algo-cli · verified execution</span>
              <span>local</span>
            </div>
            <div className="terminal-body">
              <CopyInstall command="pipx install algo-cli-runtime" />
              <CopyInstall command="algo-cli doctor" />
              <div className="terminal-gap" />
              <p><span className="prompt">$</span> algo-cli --oneshot --json --approval-mode auto</p>
              <p className="task-line">“fix the failing parser test and verify the result”</p>
              <p><span className="event violet">◆ inspect</span> src/parser.py · tests/test_parser.py</p>
              <p><span className="event cyan">◆ edit</span> src/parser.py <span className="muted">+4 −2</span></p>
              <p><span className="event lime">✓ verify</span> pytest <span className="muted">148 passed</span></p>
              <p><span className="event lime">✓ evidence</span> tracked diff · file scope intact</p>
              <div className="terminal-gap" />
              <p><span className="done">done</span> complete <span className="muted">· verified delta</span><i className="caret" /></p>
            </div>
          </div>
        </div>
      </section>

      <section className="proof-rail" aria-label="Algo CLI evidence">
        <div className="proof-wide">
          <span className="data-label">Reported local benchmark</span>
          <div className="benchmark-line"><strong><em>9/9</em> objective passes</strong><div className="mini-bars">{Array.from({ length: 9 }, (_, i) => <i key={i} />)}</div></div>
          <small>Three tasks × three repetitions · same model and machine</small>
        </div>
        <div><span className="data-label">Median runtime</span><strong>33.900s</strong><small>27-run comparison</small></div>
        <div><span className="data-label">Core posture</span><strong>Local first</strong><small>Remote services optional</small></div>
      </section>

      <section className="section-block" id="system">
        <SectionHeading eyebrow="THE ALGO LOOP" title="One runtime. Four disciplines." text="The product is organized around a simple operating loop: act deliberately, retain useful context, route only when complexity earns it, and verify before claiming completion." />
        <div className="pillar-grid">
          {pillars.map((pillar) => (
            <article className={`pillar-card ${pillar.accent}`} key={pillar.name}>
              <span className="pillar-index">{pillar.index} / SYSTEM</span>
              <div className="pillar-glyph">{pillar.name === "Act" ? ">_" : pillar.name === "Remember" ? "∞" : pillar.name === "Route" ? "◇" : "✓"}</div>
              <h3>{pillar.name}</h3>
              <p>{pillar.text}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="split-section">
        <div>
          <SectionHeading eyebrow="BUILT FOR REAL WORK" title="Deep when needed. Quiet when not." text="Algo CLI keeps simple tasks simple while exposing structured paths for larger, higher-risk work." />
          <div className="feature-list">
            <article><span>AGENT / 01</span><h3>Bounded multi-agent teams</h3><p>Read-only specialists gather evidence in parallel. One integration pipeline owns writes, review, and verification.</p></article>
            <article><span>MEMORY / 02</span><h3>Durable context with consent</h3><p>Memories, lessons, skills, and code retrieval have visible controls and privacy-safe defaults.</p></article>
            <article><span>HARNESS / 03</span><h3>Searchable runtime knowledge</h3><p>Use packaged public docs by default. Add local agent stores only when you explicitly opt in.</p></article>
          </div>
        </div>
        <aside className="knowledge-panel">
          <span className="data-label">PUBLIC KNOWLEDGE PLANE</span>
          <h2>Useful to the harness.<br />Never required by it.</h2>
          <p>The website exposes lean, versioned resources that agents can consume without scraping decorative HTML.</p>
          <ul className="endpoint-list">
            <li><a href="/llms.txt"><code>/llms.txt</code><span>agent-readable map</span></a></li>
            <li><a href="/docs/index.json"><code>/docs/index.json</code><span>versioned document index</span></a></li>
            <li><a href="/api/v1/releases/stable.json"><code>/api/v1/releases/stable.json</code><span>release channel status</span></a></li>
            <li><a href="/benchmarks/summary.json"><code>/benchmarks/summary.json</code><span>scoped benchmark evidence</span></a></li>
          </ul>
          <Link className="text-link" href="/docs#machine-readable">Read the machine interface contract →</Link>
        </aside>
      </section>

      <section className="claim-strip">
        <div><span className="status-dot" /> EVIDENCE, NOT HYPE</div>
        <p>Algo CLI ranked first in our reported 27-run local comparison. That is a scoped result—not independently reproduced evidence or a universal superiority claim.</p>
        <Link href="/benchmarks">Inspect the methodology →</Link>
      </section>

      <Footer />
    </main>
  );
}
