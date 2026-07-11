import { PageFrame, Pill } from "../site-chrome";

export const metadata = { title: "Benchmarks" };

const rows = [
  ["Algo CLI", "9/9", "9/9", "33.900s", "64.473s"],
  ["Pi", "6/9", "9/9", "34.005s", "39.568s"],
  ["OpenCode", "6/9", "9/9", "59.260s", "165.592s"],
];

export default function BenchmarksPage() {
  return <PageFrame eyebrow="EVIDENCE / 2026-07-11" title="Show the receipts." intro="This aggregate report describes a small local comparison and is explicit about what has not yet been published or independently reproduced.">
    <section className="content-wrap">
      <div className="benchmark-summary"><div><Pill tone="lime">REPORTED LOCAL RANK 01</Pill><strong>9/9</strong><span>reported Algo CLI checker passes</span></div><p>Three draft tasks, three repetitions per harness, one local Qwen model, one machine, and balanced run order. Every harness occupied positions one, two, and three exactly once per task.</p></div>
      <div className="table-wrap"><table><thead><tr><th>Harness</th><th>Objective</th><th>Clean runs</th><th>Median</th><th>p95</th></tr></thead><tbody>{rows.map((row) => <tr className={row[0] === "Algo CLI" ? "winner" : ""} key={row[0]}>{row.map((cell, i) => <td key={cell}>{i === 0 && row[0] === "Algo CLI" ? <><span className="status-dot" /> {cell}</> : cell}</td>)}</tr>)}</tbody></table></div>
      <div className="method-grid"><article><span>01 / SAME MODEL</span><h3>Harness behavior, not model selection</h3><p>Every run used <code>qwen3.6:35b-mlx</code> through local Ollama on the same machine.</p></article><article><span>02 / FRESH STATE</span><h3>Isolated runs</h3><p>Each cell received a fresh workspace and user/session state. Fixtures and source tasks were hashed before and after.</p></article><article><span>03 / BALANCED ORDER</span><h3>Position bias controlled</h3><p>A Latin-square rotation placed every task/harness combination in each run position once.</p></article></div>
      <div className="claim-guard"><h2>What this supports</h2><p>“Algo CLI ranked first in the reported balanced 27-run local comparison.”</p><h2>What this does not support</h2><p>Independent reproduction, universal superiority, meaningful speed leadership over Pi, or greater underlying model power. Raw run artifacts and competitor revisions are not yet public; the suite is also small and uses only one model and machine.</p><a className="button secondary" href="/benchmarks/summary.json">Open the aggregate JSON →</a></div>
    </section>
  </PageFrame>;
}
