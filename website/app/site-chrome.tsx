import Link from "next/link";

export function Header() {
  return (
    <header className="site-header">
      <Link className="brand" href="/" aria-label="Algo CLI home"><span className="brand-mark">&gt;_</span><b>algo-cli</b><em>/ runtime</em></Link>
      <nav aria-label="Primary navigation">
        <Link href="/docs">Docs</Link><Link href="/install">Install</Link><Link href="/doctor">Doctor</Link><Link href="/benchmarks">Benchmarks</Link><Link href="/security">Security</Link>
      </nav>
      <a className="header-github" href="https://github.com/Seabass-up/Algo-cli" rel="noreferrer">GitHub ↗</a>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="site-footer">
      <div><span className="brand-mark small">&gt;_</span><strong>Algo CLI</strong><p>Verified work. Local control.</p></div>
      <div className="footer-links"><Link href="/docs">Docs</Link><Link href="/install">Install</Link><Link href="/benchmarks">Benchmarks</Link><Link href="/security">Security</Link><a href="/llms.txt">llms.txt</a></div>
      <div className="footer-status"><span className="status-dot" /> RELEASE CANDIDATE · v0.14.0<br /><small>MIT · Python 3.10+</small></div>
    </footer>
  );
}

export function PageFrame({ eyebrow, title, intro, children }: { eyebrow: string; title: string; intro: string; children: React.ReactNode }) {
  return <main className="site-shell"><Header /><section className="page-hero grid-surface"><div className="eyebrow"><span />{eyebrow}</div><h1>{title}</h1><p>{intro}</p></section>{children}<Footer /></main>;
}

export function SectionHeading({ eyebrow, title, text }: { eyebrow: string; title: string; text: string }) {
  return <div className="section-heading"><span>{eyebrow}</span><h2>{title}</h2><p>{text}</p></div>;
}

export function Pill({ tone, children }: { tone: "lime" | "cyan" | "violet"; children: React.ReactNode }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}
