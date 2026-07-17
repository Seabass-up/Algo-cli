---
title: Provider Authentication Recovery
description: Recovery runbook for invalidated OAuth sessions and provider configuration failures.
tags: [provider, oauth, authentication, recovery, operations]
status: active
updated: 2026-07-17
---

# Provider Authentication Recovery

Treat HTTP 401, invalidated-token, expired-token, and refresh failures as authentication state problems rather than model failures. Algo CLI should attempt one bounded refresh when a refresh credential exists, persist the replacement atomically, and retry once. It must not loop indefinitely or print credentials.

If recovery fails, clear only the invalid access session and direct the operator to the provider-specific setup command. Use `algo-cli config setup <provider>` for guided setup and `algo-cli config auth <provider> login` when a new login is required. Confirm provider readiness before retrying a model request.

For model-not-found errors, distinguish authentication from model routing: resolve aliases to provider model IDs, check that the authenticated account exposes the model, and report the selected provider and resolved model without leaking tokens.
