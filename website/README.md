# algo-cli.com

The public knowledge and trust plane for Algo CLI.

The site provides human documentation, release status, benchmark evidence, a local-only diagnostic decoder, and lean machine-readable resources. It is deliberately not required for core Algo CLI operation and does not receive prompts, files, memories, or identity records from the CLI.

## Routes

- `/` — product overview
- `/install` — release-aware installation guide
- `/docs` — command and runtime field guide
- `/doctor` — browser-local diagnostic decoder
- `/benchmarks` — scoped evidence and methodology
- `/security` — trust boundaries and disclosure guidance
- `/llms.txt` — agent-readable site map
- `/api/v1/releases/stable.json` — release-channel status
- `/docs/index.json` — machine-readable document catalog
- `/benchmarks/summary.json` — benchmark summary and limitations

## Development

```bash
npm install
npm run dev
npm test
```

## Dependency security

Next.js and its lint configuration are pinned together. The Sharp override keeps
both Next.js and Miniflare on the patched decoder while Miniflare still pins an
older version. Remove the override only after the complete resolved tree is
patched and the native-image controls pass. See the upstream
[Next.js advisory](https://github.com/vercel/next.js/security/advisories/GHSA-2xp9-vwfh-vxw4)
and [Sharp advisory](https://github.com/lovell/sharp/security/advisories/GHSA-rgj7-g3m4-5g8c).

`npm test` verifies the resolved decoders and benign AVIF conversion, plus the
YAML empty-merge work budget and ordinary inheritance. `npm audit` remains a
separate blocking gate; the focused controls do not replace it or prove that a
deployed environment is exploitable. The YAML control follows the upstream
[merge-budget advisory](https://github.com/nodeca/js-yaml/security/advisories/GHSA-2883-xcg3-v3hh).

## Cloudflare publication

The production Worker owns both `algo-cli.com` and `www.algo-cli.com` as
Cloudflare Custom Domains. Cloudflare creates their DNS records and TLS
certificates during deployment.

```bash
npm run deploy:cloudflare:dry-run
npm run deploy:cloudflare
```
