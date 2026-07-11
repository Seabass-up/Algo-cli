"use client";

import { useState } from "react";

export function CopyInstall({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }
  return <div className="copy-command"><p><span className="prompt">$</span> {command}</p><button type="button" onClick={copy} aria-label={`Copy ${command}`}>{copied ? "COPIED ✓" : "COPY"}</button></div>;
}
