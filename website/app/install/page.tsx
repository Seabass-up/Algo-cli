import { CopyInstall } from "../copy-install";
import { PageFrame, Pill } from "../site-chrome";

export const metadata = { title: "Install" };

export default function InstallPage() {
  return <PageFrame eyebrow="INSTALL / v0.14.0 RC" title="Start local. Add only what you need." intro="Algo CLI requires Python 3.10 or newer. Version 0.14.0 is a release candidate; install the public source today and switch to an index install after PyPI publication.">
    <section className="content-wrap">
      <div className="notice"><Pill tone="lime">PUBLIC SOURCE</Pill><p>The reviewed repository is public. Package publication is the remaining stable-release step.</p></div>
      <div className="install-grid">
        <article className="install-card featured"><span>01 / PUBLIC SOURCE</span><h2>Install from the repository</h2><p>Clone the public repository, then let Pipx isolate the application and its dependencies.</p><CopyInstall command="git clone https://github.com/Seabass-up/Algo-cli.git && cd Algo-cli" /><CopyInstall command="pipx install . && algo-cli doctor" /></article>
        <article className="install-card"><span>02 / AFTER PYPI RELEASE</span><h2>Stable Pipx install</h2><p>This command becomes active when the release manifest reports that the package is published.</p><CopyInstall command="pipx install algo-cli" /><CopyInstall command="algo-cli doctor" /></article>
        <article className="install-card"><span>03 / AFTER PYPI RELEASE</span><h2>Fast uv tool install</h2><p>Use Astral uv after the package is available from the public index.</p><CopyInstall command="uv tool install algo-cli" /><CopyInstall command="algo-cli" /></article>
      </div>
      <div className="provider-grid">
        <article><span>LOCAL OLLAMA</span><h3>Keep inference on your machine</h3><CopyInstall command="ollama pull qwen3" /><CopyInstall command="algo-cli" /></article>
        <article><span>CHATGPT / CODEX</span><h3>Authenticate once in the CLI</h3><CopyInstall command="algo-cli --model gpt-5.5" /><p className="fine-print">Run <code>/chatgpt-login</code> when prompted.</p></article>
        <article><span>ONE-SHOT / CI</span><h3>Emit framed NDJSON events</h3><CopyInstall command={'algo-cli --oneshot --json "summarize this folder"'} /><p className="fine-print">The first event is <code>session_start</code>; the last is <code>done</code>.</p></article>
      </div>
    </section>
  </PageFrame>;
}
