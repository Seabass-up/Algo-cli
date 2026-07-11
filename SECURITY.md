# Security policy

## Supported versions

Security fixes are provided for the latest published release. Upgrade before reporting an issue that only affects an older release.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not open a public issue for exposed credentials, unsafe command execution, authentication bypasses, or privacy leaks.

Include the affected version, operating system, reproduction steps, impact, and any suggested mitigation. Remove tokens, personal data, and private file contents from screenshots and logs.

## Security model

Algo CLI can read files, execute approved tools, and connect to local or cloud model providers. Safe mode, approval prompts, and content redaction reduce risk but are not operating-system sandboxes. Review commands before approval and keep `~/.algo_cli` private.

External harness stores and index-compute-lab are opt-in. When enabled, retrieved local context can be included in cloud-provider requests; see [Privacy and local context](docs/privacy-and-context.md).
