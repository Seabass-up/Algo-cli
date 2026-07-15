import type { CSSProperties } from "react";
import benchmark from "../../public/benchmarks/summary.json";
import tokenEfficiency from "../../public/benchmarks/token-efficiency.json";
import { PageFrame, Pill } from "../site-chrome";

const protocol = benchmark.protocol;
const rows = benchmark.results;
const algo = rows.find((row) => row.id === "algo-cli");
if (!algo) throw new Error("benchmark data is missing Algo CLI");

const evidenceDate = benchmark.created_at.slice(0, 10);
const perfectRows = rows.filter((row) => row.clean_runs === row.runs);
const codingReductions = tokenEfficiency.coding_scenarios.map((scenario) => scenario.schema_reduction_pct);
const minimumCodingReduction = Math.min(...codingReductions);
const maximumCodingReduction = Math.max(...codingReductions);
const tokenRange = `${minimumCodingReduction.toFixed(1)}–${maximumCodingReduction.toFixed(1)}%`;

export const metadata = {
  title: "Benchmarks",
  description: `A reproducible, same-model draft comparison of Algo CLI and ten terminal agent harnesses across ${protocol.total_runs} controlled runs.`,
};

type Result = (typeof benchmark.results)[number];

const percent = (value: number, total: number) => Math.round((value / total) * 100);

function ReliabilityChart({ values }: { values: Result[] }) {
  return (
    <div className="bench-chart" role="img" aria-label={`Verified-run rate for eleven agent harnesses across ${protocol.runs_per_harness} runs each`}>
      <div className="chart-axis" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div>
      {values.map((row) => {
        const rate = percent(row.clean_runs, row.runs);
        return <div className={`bar-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
          <span className="bar-label"><b>{row.harness}</b><small>{row.clean_runs}/{row.runs} verified</small></span>
          <div className="bar-track"><i style={{ width: `${rate}%` }} /><span style={{ left: `${rate}%` }}>{rate}%</span></div>
        </div>;
      })}
    </div>
  );
}

function LatencyChart({ values }: { values: Result[] }) {
  const max = protocol.timeout_seconds;
  return (
    <div className="latency-chart" role="img" aria-label={`Median and 95th percentile runtime by harness on a zero to ${max} second scale`}>
      <div className="latency-legend"><span><i className="median-key" /> Median</span><span><i className="p95-key" /> p95</span><em>seconds · lower is better after quality gates</em></div>
      <div className="latency-axis" aria-hidden="true"><span>0</span><span>{max / 2}</span><span>{max}s</span></div>
      {values.map((row) => {
        const style = { "--median": `${Math.min(100, row.median_seconds / max * 100)}%`, "--p95": `${Math.min(100, row.p95_seconds / max * 100)}%` } as CSSProperties;
        return <div className={`latency-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
          <span><b>{row.harness}</b><small>{row.median_seconds.toFixed(1)}s / {row.p95_seconds.toFixed(1)}s</small></span>
          <div className="latency-track" style={style}><i className="median-dot" /><i className="p95-dot" /></div>
        </div>;
      })}
    </div>
  );
}

function TaskMatrix({ values }: { values: Result[] }) {
  return <div className="task-matrix">
    <div className="matrix-head"><span>Harness</span>{protocol.tasks.map((task) => <span key={task.id}>{task.short_label}</span>)}</div>
    {values.map((row) => <div className={`matrix-row ${row.id === "algo-cli" ? "is-algo" : ""}`} key={row.id}>
      <span><b>{row.harness}</b><small>rank {String(row.rank).padStart(2, "0")}</small></span>
      {protocol.tasks.map((task) => {
        const value = row.task_passes[task.id as keyof typeof row.task_passes];
        return <span className={`matrix-score score-${value}`} aria-label={`${row.harness}, ${task.label}: ${value} of ${protocol.repetitions_per_cell} passes`} key={task.id}><i />{value}/{protocol.repetitions_per_cell}</span>;
      })}
    </div>)}
  </div>;
}

function TokenEfficiencyChart() {
  const values = [
    { label: `Full ${tokenEfficiency.catalog_tool_count}-tool catalog`, tokens: tokenEfficiency.full_schema_tokens },
    ...tokenEfficiency.coding_scenarios.map((scenario) => ({ label: scenario.label, tokens: scenario.selected_schema_tokens })),
  ];
  const axisMax = Math.ceil(tokenEfficiency.full_schema_tokens / 1000) * 1000;
  return <div className="token-efficiency-chart" role="img" aria-label="Estimated tool-schema tokens for the full catalog and two coding scenarios">
    <div className="token-chart-axis" aria-hidden="true"><span>0</span><span>{(axisMax / 2).toLocaleString()}</span><span>{axisMax.toLocaleString()} tokens</span></div>
    {values.map((row, index) => {
      const width = row.tokens / tokenEfficiency.full_schema_tokens * 100;
      return <div className={`token-row ${index === 0 ? "is-baseline" : "is-selected"}`} key={row.label}>
        <span><b>{row.label}</b><small>{row.tokens.toLocaleString()} estimated tokens</small></span>
        <div><i style={{ width: `${width}%` }} /></div>
      </div>;
    })}
  </div>;
}

export default function BenchmarksPage() {
  const perfectText = perfectRows.length
    ? `${perfectRows.length} harness${perfectRows.length === 1 ? "" : "es"} completed every run with checker, process, and scope gates intact.`
    : "No harness completed every run with all verification gates intact.";
  return <PageFrame eyebrow={`EVIDENCE / ${evidenceDate}`} title="Benchmark the harness. Not the hype." intro={`A warmed, same-model draft comparison of eleven terminal agent harnesses across ${protocol.total_runs} fresh-state runs—with correctness, scope, process health, and latency kept separate.`}>
    <section className="content-wrap benchmark-page">
      <div className="benchmark-hero-card">
        <div>
          <Pill tone={algo.clean_runs === algo.runs ? "lime" : "cyan"}>{algo.clean_runs === algo.runs ? "TOP RELIABILITY GROUP" : "VERIFIED RESULT"}</Pill>
          <strong>{algo.clean_runs}/{algo.runs}</strong>
          <span>Algo CLI verified runs</span>
        </div>
        <div className="benchmark-kpis">
          <article><span>Measured runs</span><b>{protocol.total_runs}</b><small>{protocol.measured_harnesses} harnesses × {protocol.tasks.length} tasks × {protocol.repetitions_per_cell} reps</small></article>
          <article><span>Objective rank</span><b>#{String(algo.rank).padStart(2, "0")}</b><small>quality gates precede latency</small></article>
          <article><span>Algo median</span><b>{algo.median_seconds.toFixed(1)}s</b><small>p95 {algo.p95_seconds.toFixed(1)} seconds</small></article>
        </div>
        <p>{benchmark.claim} {perfectText}</p>
      </div>

      <section className="benchmark-section" aria-labelledby="token-title">
        <div className="benchmark-section-head"><div><span>01 / CODING TOKEN COST</span><h2 id="token-title">Coding starts with {tokenRange} less tool context.</h2></div><p>Instead of sending all {tokenEfficiency.catalog_tool_count} action schemas on every turn, Algo CLI ranks a bounded task-relevant catalog. These deterministic estimates cover harness-supplied schema context; they are not provider billing or model-output tokens.</p></div>
        <TokenEfficiencyChart />
        <div className="token-proof-grid">
          {tokenEfficiency.coding_scenarios.map((scenario) => <article key={scenario.id}><span>{scenario.label}</span><b>{scenario.schema_reduction_pct.toFixed(1)}% fewer</b><small>{tokenEfficiency.full_schema_tokens.toLocaleString()} → {scenario.selected_schema_tokens.toLocaleString()} schema tokens</small></article>)}
          <article><span>Required-tool recall</span><b>{tokenEfficiency.all_scenarios.required_tools_recalled} / {tokenEfficiency.all_scenarios.required_tools_total}</b><small>{tokenEfficiency.all_scenarios.count} scenarios · {tokenEfficiency.repeats} repeats</small></article>
        </div>
        <p className="token-scope-note">Median reduction across all nine scenarios was {tokenEfficiency.all_scenarios.median_schema_reduction_pct.toFixed(1)}%; typed-program intermediate context fell {tokenEfficiency.typed_program.reduction_pct.toFixed(1)}%. These results have not been independently reproduced. <a href="/benchmarks/token-efficiency.json">Inspect the JSON evidence →</a></p>
      </section>

      <section className="benchmark-section" aria-labelledby="reliability-chart-title">
        <div className="benchmark-section-head"><div><span>02 / VERIFIED RUNS</span><h2 id="reliability-chart-title">Correctness, process, and scope all count.</h2></div><p>A verified run requires the external checker to pass, the harness process to finish cleanly, baseline failure to be proven, protected inputs to remain intact, and every workspace edit to stay in the task allowlist.</p></div>
        <ReliabilityChart values={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="latency-title">
        <div className="benchmark-section-head"><div><span>03 / LATENCY</span><h2 id="latency-title">Tail latency stays visible.</h2></div><p>Ranking considers median duration only after checker, scope, and process rates. p95 exposes slow or timed-out runs that a median can hide; the shared model warmup is excluded from every score.</p></div>
        <LatencyChart values={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="tasks-title">
        <div className="benchmark-section-head"><div><span>04 / TASK CELLS</span><h2 id="tasks-title">Four distinct evidence problems.</h2></div><p>Each harness receives the same frozen prompts and fixtures. The matrix reports checker passes, not subjective answer grades.</p></div>
        <TaskMatrix values={rows} />
      </section>

      <section className="benchmark-section" aria-labelledby="ranking-title">
        <div className="benchmark-section-head"><div><span>05 / EXACT RESULTS</span><h2 id="ranking-title">The complete ranked table.</h2></div><p>Ranking sorts checker pass rate, scope pass rate, clean-process rate, then median duration. Speed cannot outrank correctness or scope discipline.</p></div>
        <div className="table-wrap benchmark-table"><table><thead><tr><th>Rank</th><th>Harness</th><th>Checker</th><th>Verified</th><th>Scope</th><th>Median</th><th>p95</th></tr></thead><tbody>{rows.map((row) => <tr className={row.id === "algo-cli" ? "winner" : ""} key={row.id}><td>{String(row.rank).padStart(2, "0")}</td><td>{row.id === "algo-cli" && <span className="status-dot" />}{row.harness}</td><td>{row.passes}/{row.runs}</td><td>{row.clean_runs}/{row.runs}</td><td>{row.scope_passes}/{row.runs}</td><td>{row.median_seconds.toFixed(1)}s</td><td>{row.p95_seconds.toFixed(1)}s</td></tr>)}</tbody></table></div>
      </section>

      <section className="methodology-panel" aria-labelledby="method-title">
        <div><span>06 / PROTOCOL</span><h2 id="method-title">Same model. Warm start. Fresh state.</h2><p>Every scored run used local Ollama with <code>{protocol.same_model}</code> on {benchmark.environment.hardware}, a {protocol.timeout_seconds}-second cap, isolated harness state, identical task fixtures, and deterministic cyclic rotation. Host OS: {benchmark.environment.operating_system}.</p><p><code>task sha256:{protocol.task_suite_sha256}</code></p></div>
        <ol>{protocol.tasks.map((task) => <li key={task.id}><b>{task.label}</b><span>{task.description}</span></li>)}</ol>
      </section>

      <section className="unscored-panel" aria-labelledby="unscored-title">
        <div className="benchmark-section-head"><div><span>07 / NOT SCORED</span><h2 id="unscored-title">Blocked is not zero.</h2></div><p>Products without a deterministic, authorized headless path were excluded instead of receiving invented scores.</p></div>
        <div className="unscored-grid">{benchmark.blocked_or_non_comparable.map((item) => <article key={item.product}><b>{item.product}</b><p>{item.reason}</p></article>)}</div>
      </section>

      <div className="claim-guard benchmark-claim"><h2>What the evidence supports</h2><p>{benchmark.claim} Separately: {tokenEfficiency.claim}</p><h2>What it does not support</h2><p>{benchmark.limitations} {tokenEfficiency.limitations}</p><div className="benchmark-downloads"><a className="button secondary" href="/benchmarks/summary.json">Harness JSON →</a><a className="button secondary" href="/benchmarks/token-efficiency.json">Token JSON →</a><a className="button secondary" href="/benchmarks/results.csv">Results CSV →</a><a className="text-link" href={`https://github.com/Seabass-up/Algo-cli/tree/${benchmark.source_revision}/benchmarks/competitors`} rel="noreferrer">Inspect the benchmark suite ↗</a></div></div>
    </section>
  </PageFrame>;
}
