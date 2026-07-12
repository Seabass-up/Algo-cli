import type { CSSProperties } from "react";
import benchmark from "../../public/benchmarks/summary.json";
import { PageFrame, Pill } from "../site-chrome";

export const metadata = {
  title: "Benchmarks",
  description: "A reproducible, same-model comparison of Algo CLI and ten terminal agent harnesses across 99 controlled runs.",
};

type Result = (typeof benchmark.results)[number];

const percent = (value: number, total: number) => Math.round((value / total) * 100);

function ReliabilityChart({ rows }: { rows: Result[] }) {
  return (
    <div className="bench-chart" role="img" aria-label="Clean-run rate for eleven agent harnesses across nine runs each">
      <div className="chart-axis" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div>
      {rows.map((row) => {
        const rate = percent(row.clean_runs, row.runs);
        return <div className={`bar-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
          <span className="bar-label"><b>{row.harness}</b><small>{row.clean_runs}/{row.runs} clean</small></span>
          <div className="bar-track"><i style={{ width: `${rate}%` }} /><span style={{ left: `${rate}%` }}>{rate}%</span></div>
        </div>;
      })}
    </div>
  );
}

function LatencyChart({ rows }: { rows: Result[] }) {
  const max = 300;
  return (
    <div className="latency-chart" role="img" aria-label="Median and 95th percentile runtime by harness on a zero to three hundred second scale">
      <div className="latency-legend"><span><i className="median-key" /> Median</span><span><i className="p95-key" /> p95</span><em>seconds · lower is better</em></div>
      <div className="latency-axis" aria-hidden="true"><span>0</span><span>150</span><span>300s</span></div>
      {rows.map((row) => {
        const style = { "--median": `${Math.min(100, row.median_seconds / max * 100)}%`, "--p95": `${Math.min(100, row.p95_seconds / max * 100)}%` } as CSSProperties;
        return <div className={`latency-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
          <span><b>{row.harness}</b><small>{row.median_seconds.toFixed(1)}s / {row.p95_seconds.toFixed(1)}s</small></span>
          <div className="latency-track" style={style}><i className="median-dot" /><i className="p95-dot" /></div>
        </div>;
      })}
    </div>
  );
}

function TaskMatrix({ rows }: { rows: Result[] }) {
  const tasks = [
    ["code_repair_passes", "Code repair"],
    ["tool_trap_passes", "Safety trap"],
    ["memory_passes", "Memory conflict"],
  ] as const;
  return <div className="task-matrix">
    <div className="matrix-head"><span>Harness</span>{tasks.map(([, label]) => <span key={label}>{label}</span>)}</div>
    {rows.map((row) => <div className={`matrix-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
      <span><b>{row.harness}</b><small>rank {String(row.rank).padStart(2, "0")}</small></span>
      {tasks.map(([field, label]) => {
        const value = row[field];
        return <span className={`matrix-score score-${value}`} aria-label={`${row.harness}, ${label}: ${value} of 3 passes`} key={field}><i />{value}/3</span>;
      })}
    </div>)}
  </div>;
}

export default function BenchmarksPage() {
  const rows = benchmark.results;
  return <PageFrame eyebrow="EVIDENCE / 2026-07-11" title="Benchmark the harness. Not the hype." intro="A controlled, same-model comparison of eleven terminal agent harnesses across 99 fresh-state runs—with correctness, scope, and latency kept separate.">
    <section className="content-wrap benchmark-page">
      <div className="benchmark-hero-card">
        <div>
          <Pill tone="lime">TOP RELIABILITY GROUP</Pill>
          <strong>9/9</strong>
          <span>Algo CLI clean runs</span>
        </div>
        <div className="benchmark-kpis">
          <article><span>Measured runs</span><b>99</b><small>11 harnesses × 3 tasks × 3 reps</small></article>
          <article><span>Objective rank</span><b>#03</b><small>latency breaks the six-way tie</small></article>
          <article><span>Algo median</span><b>66.8s</b><small>p95 89.4 seconds</small></article>
        </div>
        <p>Algo CLI tied Pi, OpenCode, Goose, OpenClaw, and Hermes Agent at 100% clean reliability. Pi and OpenCode ranked ahead only because their median runtime was lower.</p>
      </div>

      <section className="benchmark-section" aria-labelledby="reliability-title">
        <div className="benchmark-section-head"><div><span>01 / RELIABILITY</span><h2 id="reliability-title">Six harnesses went 9 for 9.</h2></div><p>Clean means more than the final answer: the external checker passed, the process exited cleanly, structured output was valid, protected inputs stayed unchanged, and no out-of-scope files were edited.</p></div>
        <ReliabilityChart rows={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="latency-title">
        <div className="benchmark-section-head"><div><span>02 / LATENCY</span><h2 id="latency-title">Reliability did not mean equal speed.</h2></div><p>Pi had the lowest median among perfect scorers. Algo sat in the middle of the perfect group, while Goose and several competitors showed wide tail latency.</p></div>
        <LatencyChart rows={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="tasks-title">
        <div className="benchmark-section-head"><div><span>03 / TASK CELLS</span><h2 id="tasks-title">The safety trap separated the field.</h2></div><p>Every measured harness solved the small code repair. The misleading-state trap exposed inconsistent file selection; Droid also struggled to reconcile stale retrieved context against live files.</p></div>
        <TaskMatrix rows={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="ranking-title">
        <div className="benchmark-section-head"><div><span>04 / EXACT RESULTS</span><h2 id="ranking-title">The complete ranked table.</h2></div><p>Ranking sorts checker pass rate, scope pass rate, clean-run rate, then median duration. Speed cannot outrank correctness or scope discipline.</p></div>
        <div className="table-wrap benchmark-table"><table><thead><tr><th>Rank</th><th>Harness</th><th>Checker</th><th>Clean</th><th>Scope</th><th>Median</th><th>p95</th></tr></thead><tbody>{rows.map((row) => <tr className={row.id === "algo-cli" ? "winner" : ""} key={row.id}><td>{String(row.rank).padStart(2, "0")}</td><td>{row.id === "algo-cli" && <span className="status-dot" />}{row.harness}</td><td>{row.passes}/{row.runs}</td><td>{row.clean_runs}/{row.runs}</td><td>{row.scope_passes}/{row.runs}</td><td>{row.median_seconds.toFixed(1)}s</td><td>{row.p95_seconds.toFixed(1)}s</td></tr>)}</tbody></table></div>
      </section>

      <section className="methodology-panel" aria-labelledby="method-title">
        <div><span>05 / PROTOCOL</span><h2 id="method-title">Same model. Fresh state. External checks.</h2><p>Every scored run used local Ollama with <code>qwen3.6:35b-mlx</code> on the same machine, a 300-second timeout, isolated harness state, identical task fixtures, and deterministic cyclic order rotation.</p></div>
        <ol>
          <li><b>Code repair</b><span>Small Python repository with a failing test and minimal source fix.</span></li>
          <li><b>Safety trap</b><span>Misleading docs and a protected decoy config test evidence selection.</span></li>
          <li><b>Memory conflict</b><span>Live project files must override stale retrieved context.</span></li>
        </ol>
      </section>

      <section className="unscored-panel" aria-labelledby="unscored-title">
        <div className="benchmark-section-head"><div><span>06 / NOT SCORED</span><h2 id="unscored-title">Blocked is not zero.</h2></div><p>Products without a deterministic, authorized headless path were excluded instead of receiving invented scores.</p></div>
        <div className="unscored-grid">{benchmark.blocked_or_non_comparable.map((item) => <article key={item.product}><b>{item.product}</b><p>{item.reason}</p></article>)}</div>
      </section>

      <div className="claim-guard benchmark-claim"><h2>What the evidence supports</h2><p>“Algo CLI achieved 9/9 clean runs and tied the top reliability group in our three-task, same-model benchmark.”</p><h2>What it does not support</h2><p>A categorical better-than claim, native-model superiority, broad software-engineering coverage, desktop UX leadership, or independently reproduced results. This is a small local harness study with three repetitions per cell.</p><div className="benchmark-downloads"><a className="button secondary" href="/benchmarks/summary.json">Aggregate JSON →</a><a className="button secondary" href="/benchmarks/results.csv">Results CSV →</a><a className="text-link" href={`https://github.com/Seabass-up/Algo-cli/tree/${benchmark.source_revision}/benchmarks/competitors`} rel="noreferrer">Inspect the benchmark suite ↗</a></div></div>
    </section>
  </PageFrame>;
}
