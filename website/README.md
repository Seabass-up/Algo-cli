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
