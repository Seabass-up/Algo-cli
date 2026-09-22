import { CopyInstall } from "../copy-install";
import { PageFrame, Pill } from "../site-chrome";

export const metadata = { title: "Install" };

export default function InstallPage() {
  return <PageFrame eyebrow="INSTALL / v0.20.0" title="Start local. Add only what you need." intro="Algo CLI v0.20.0 is available from PyPI for Python 3.10 or newer. The algo-cli-runtime distribution installs the algo-cli command.">
    <section className="content-wrap">
      <div className="notice"><Pill tone="lime">STABLE RELEASE</Pill><p>Version 0.20.0 is published from the immutable v0.20.0 tag through the protected release workflow. Wheel and sdist digests are listed in the <a href="/api/v1/releases/stable.json">release manifest</a>.</p></div>
      <div className="install-grid">
        <article className="install-card featured"><span>01 / RECOMMENDED</span><h2>Stable Pipx install</h2><p>Install the isolated distribution, then verify local readiness.</p><CopyInstall command="pipx install algo-cli-runtime" /><CopyInstall command="algo-cli doctor" /></article>
        <article className="install-card"><span>02 / FAST INSTALL</span><h2>Astral uv</h2><p>Use uv for a fast isolated tool installation.</p><CopyInstall command="uv tool install algo-cli-runtime" /><CopyInstall command="algo-cli" /></article>
        <article className="install-card"><span>03 / UPGRADE</span><h2>Already installed</h2><p>Update in place from 0.19.2. Configuration, credentials, memory, and workspace settings are preserved. A 0.19.1 install in a pip-free <code>uv pip</code> environment cannot self-update; run <code>uv pip install --upgrade algo-cli-runtime</code> there once.</p><CopyInstall command="algo-cli update" /><CopyInstall command="algo-cli --version" /></article>
        <article className="install-card"><span>04 / SOURCE</span><h2>Reviewed repository</h2><p>Check out the immutable v0.20.0 tag when you want to inspect the exact source. Its release page carries the source archive, SBOM, provenance, and Sigstore bundles.</p><CopyInstall command="git clone https://github.com/Seabass-up/Algo-cli.git && cd Algo-cli && git checkout v0.20.0" /><CopyInstall command="pipx install . && algo-cli doctor" /></article>
      </div>
      <div className="provider-grid">
        <article><span>LOCAL OLLAMA</span><h3>Keep inference on your machine</h3><CopyInstall command="ollama pull qwen3" /><CopyInstall command="algo-cli" /></article>
        <article><span>CHATGPT / CODEX</span><h3>Authenticate outside the chat REPL</h3><CopyInstall command="algo-cli config setup chatgpt" /><p className="fine-print">Then select a Codex model with <code>algo-cli --model gpt-5.6-sol</code>.</p></article>
        <article><span>ONE-SHOT / CI</span><h3>Emit framed NDJSON events</h3><CopyInstall command={'algo-cli --oneshot --json "summarize this folder"'} /><p className="fine-print">The first event is <code>session_start</code>; the last is <code>done</code>.</p></article>
      </div>
      <div className="provider-grid">
        <article><span>YOUR LIBRARY</span><h3>Bring your own catalog and kernels</h3><CopyInstall command="algo-cli" /><CopyInstall command="/intelligence init" /><p className="fine-print">New in 0.20.0: the package ships no personal pattern catalog or kernels. <code>/intelligence init</code> creates an empty <code>~/.algo_cli/ALGO.md</code> and <code>kernels.json</code> for you to fill; <code>/kernel help</code> shows the format.</p></article>
        <article><span>MEMORY</span><h3>Native Continuum, no fallback</h3><CopyInstall command="/memory doctor" /><p className="fine-print">Memory routes only through the separately installed <code>continuum-memory</code> command, selected by setting <code>continuum_enabled</code> to <code>true</code> in <code>~/.algo_cli/config.json</code>; <code>/memory doctor</code> then verifies both scopes. Upgrading from a configuration that still names a retired backend? Run <code>algo-cli config memory repair</code>.</p></article>
        <article><span>JEV (OPTIONAL)</span><h3>Advisory decisions</h3><CopyInstall command="algo-cli config jev enable --cli /absolute/path/to/jev-workflows" /><p className="fine-print">Bounded question contracts through the separately installed companion. Lint is local; inference needs this explicit setup and session approval, and answers never authorize actions.</p></article>
      </div>
    </section>
  </PageFrame>;
}
