import { CopyInstall } from "../copy-install";
import { PageFrame, Pill } from "../site-chrome";

export const metadata = { title: "Install" };

export default function InstallPage() {
  return <PageFrame eyebrow="INSTALL / v0.17.0" title="Start local. Add only what you need." intro="Algo CLI v0.17.0 is available from PyPI for Python 3.10 or newer. The algo-cli-runtime distribution installs the algo-cli command.">
    <section className="content-wrap">
      <div className="notice"><Pill tone="lime">STABLE RELEASE</Pill><p>Version 0.17.0 is published from the reviewed v0.17.0 tag through protected OIDC publishing.</p></div>
      <div className="install-grid">
        <article className="install-card featured"><span>01 / RECOMMENDED</span><h2>Stable Pipx install</h2><p>Install the isolated distribution, then verify local readiness.</p><CopyInstall command="pipx install algo-cli-runtime" /><CopyInstall command="algo-cli doctor" /></article>
        <article className="install-card"><span>02 / FAST INSTALL</span><h2>Astral uv</h2><p>Use uv for a fast isolated tool installation.</p><CopyInstall command="uv tool install algo-cli-runtime" /><CopyInstall command="algo-cli" /></article>
        <article className="install-card"><span>03 / SOURCE</span><h2>Reviewed repository</h2><p>Clone the public repository when you want to inspect the exact source before installation.</p><CopyInstall command="git clone https://github.com/Seabass-up/Algo-cli.git && cd Algo-cli" /><CopyInstall command="pipx install . && algo-cli doctor" /></article>
      </div>
      <div className="provider-grid">
        <article><span>LOCAL OLLAMA</span><h3>Keep inference on your machine</h3><CopyInstall command="ollama pull qwen3" /><CopyInstall command="algo-cli" /></article>
        <article><span>CHATGPT / CODEX</span><h3>Authenticate outside the chat REPL</h3><CopyInstall command="algo-cli config setup chatgpt" /><p className="fine-print">Then select a Codex model with <code>algo-cli --model gpt-5.6-sol</code>.</p></article>
        <article><span>ONE-SHOT / CI</span><h3>Emit framed NDJSON events</h3><CopyInstall command={'algo-cli --oneshot --json "summarize this folder"'} /><p className="fine-print">The first event is <code>session_start</code>; the last is <code>done</code>.</p></article>
      </div>
    </section>
  </PageFrame>;
}
