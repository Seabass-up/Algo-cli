"use client";

import { useMemo, useState } from "react";

type Finding = { tone: "ok" | "warn" | "error"; path: string; message: string };

function walk(value: unknown, path = "report", out: Finding[] = []): Finding[] {
  if (Array.isArray(value)) value.forEach((item, index) => walk(item, `${path}[${index}]`, out));
  else if (value && typeof value === "object") Object.entries(value as Record<string, unknown>).forEach(([key, item]) => {
    const next = `${path}.${key}`;
    const lowered = key.toLowerCase();
    if (item === false && /(ready|available|reachable|installed|configured|supported|healthy|ok)/.test(lowered)) out.push({ tone: "warn", path: next, message: "This capability reports false. Check the associated setup and whether it is optional." });
    if (typeof item === "string" && /^(error|failed|unhealthy|blocked)$/i.test(item)) out.push({ tone: "error", path: next, message: `Reported status: ${item}. Review the adjacent reason or error field.` });
    if (typeof item === "string" && /(api[_-]?key|token|secret|password)/i.test(key) && item.length > 3) out.push({ tone: "error", path: next, message: "Possible credential value detected. Remove it before sharing this report." });
    walk(item, next, out);
  });
  return out;
}

export function DoctorDecoder() {
  const [text, setText] = useState("");
  const result = useMemo(() => {
    if (!text.trim()) return { error: "", findings: [] as Finding[] };
    try { const parsed = JSON.parse(text); const findings = walk(parsed); return { error: "", findings: findings.length ? findings : [{ tone: "ok" as const, path: "report", message: "No obvious readiness failures or credential-shaped values were detected." }] }; }
    catch { return { error: "That is not valid JSON yet. Paste the JSON output from `algo-cli doctor --json`.", findings: [] as Finding[] }; }
  }, [text]);
  return <section className="content-wrap doctor-layout"><div className="doctor-input"><label htmlFor="doctor-json">REDACTED DOCTOR JSON</label><textarea id="doctor-json" value={text} onChange={(event) => setText(event.target.value)} spellCheck={false} placeholder={'{\n  "runtime": { "ready": true },\n  "provider": { "reachable": false }\n}'} /><div><button type="button" onClick={() => setText("")}>CLEAR</button><span><i className="status-dot" /> LOCAL BROWSER ANALYSIS</span></div></div><div className="doctor-output"><span className="data-label">FINDINGS</span>{result.error ? <p className="decoder-error">{result.error}</p> : result.findings.length ? result.findings.map((finding) => <article className={finding.tone} key={`${finding.path}-${finding.message}`}><strong>{finding.path}</strong><p>{finding.message}</p></article>) : <p className="muted">Results appear as you type.</p>}<div className="privacy-box"><strong>Before sharing diagnostics</strong><p>Remove usernames, home-directory paths, repository names, prompts, tokens, keys, and any file content. The safest report is the smallest report that reproduces the problem.</p></div></div></section>;
}
