import { PageFrame } from "../site-chrome";

export const metadata = { title: "Security" };

export default function SecurityPage() {
  return <PageFrame eyebrow="SECURITY / TRUST BOUNDARIES" title="Local-first is a boundary, not a slogan." intro="Algo CLI exposes tools powerful enough to change files and run commands. Its security posture starts with visible scope, explicit authority, and evidence-backed completion.">
    <section className="content-wrap security-grid">
      <article><span>01</span><h2>Approval before authority</h2><p>Write and shell operations require approval unless the user deliberately enables automatic approval. Session-only approval never persists into a new run.</p></article>
      <article><span>02</span><h2>Fail-closed verification</h2><p>Status-masking commands, unverified post-mutation claims, path escapes, and sensitive write targets are rejected instead of being treated as successful work.</p></article>
      <article><span>03</span><h2>Private context by default</h2><p>External harness stores and source-code retrieval remain off until enabled. The website never receives prompts, files, memories, or identity records from the CLI.</p></article>
      <article><span>04</span><h2>Inspectable release state</h2><p>Version, compatibility, source revision, and release-channel status are available in a stable machine-readable manifest.</p></article>
      <div className="security-report"><div><span className="status-dot" /> RESPONSIBLE DISCLOSURE</div><h2>Found a vulnerability?</h2><p>Use GitHub’s private security-advisory flow. Do not open a public issue with exploit details, credentials, private paths, or user data.</p><a className="button primary" href="https://github.com/Seabass-up/Algo-cli/security/advisories/new">Open a private advisory ↗</a></div>
    </section>
  </PageFrame>;
}
