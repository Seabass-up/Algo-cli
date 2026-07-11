import { CopyInstall } from "../copy-install";
import { PageFrame, Pill } from "../site-chrome";

export const metadata = { title: "Install" };

export default function InstallPage() {
  return <PageFrame eyebrow="INSTALL / v0.14.0 RC" title="Start local. Add only what you need." intro="Algo CLI requires Python 3.10 or newer. The public package and repository channels open with the reviewed release; invited testers can install from a trusted checkout today.">
    <section className="content-wrap">
      <div className="notice"><Pill tone="lime">RELEASE CANDIDATE</Pill><p>The codebase has cleared its release gates. Package and repository availability are the final public-release steps.</p></div>
      <div className="install-grid">
        <article className="install-card featured"><span>01 / RECOMMENDED</span><h2>Isolated application install</h2><p>Use this when the package is published. Pipx keeps the CLI separate from project environments.</p><CopyInstall command="pipx install algo-cli" /><CopyInstall command="algo-cli doctor" /></article>
        <article className="install-card"><span>02 / UV</span><h2>Fast tool install</h2><p>Use Astral uv if it is already part of your Python toolchain.</p><CopyInstall command="uv tool install algo-cli" /><CopyInstall command="algo-cli" /></article>
        <article className="install-card"><span>03 / REVIEWED CHECKOUT</span><h2>Install from source</h2><p>For maintainers and invited testers working from an authorized checkout.</p><CopyInstall command="python -m pip install ." /><CopyInstall command="algo-cli doctor" /></article>
      </div>
      <div className="provider-grid">
        <article><span>LOCAL OLLAMA</span><h3>Keep inference on your machine</h3><CopyInstall command="ollama pull qwen3" /><CopyInstall command="algo-cli" /></article>
        <article><span>CHATGPT / CODEX</span><h3>Authenticate once in the CLI</h3><CopyInstall command="algo-cli --model gpt-5.5" /><p className="fine-print">Run <code>/chatgpt-login</code> when prompted.</p></article>
        <article><span>ONE-SHOT / CI</span><h3>Emit framed NDJSON events</h3><CopyInstall command={'algo-cli --oneshot --json "summarize this folder"'} /><p className="fine-print">The first event is <code>session_start</code>; the last is <code>done</code>.</p></article>
      </div>
    </section>
  </PageFrame>;
}
