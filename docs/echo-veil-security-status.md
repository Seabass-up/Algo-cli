---
title: Echo Veil Security Status
description: Readiness contract and activation requirements for the optional Echo Veil memory layer.
tags: [echo-veil, memory, security, encryption, readiness]
status: active
updated: 2026-07-17
---

# Echo Veil Security Status

Echo Veil is optional and must remain reported as unavailable unless its package imports in the active Python runtime. An enabled flag alone does not establish readiness.

Production-ready status requires evidence for the complete write → protect → index → retrieve → persist path, restart restoration, file permissions, corruption handling, and key rotation. Readiness must identify one authoritative source and separately report installed, enabled, write-wired, retrieval-wired, and persistence-wired states.

Algo CLI must fail closed when protection was requested but cannot be initialized. It must not label metadata-only persistence as restored encrypted memory, and it must never claim that Echo Veil protects ordinary memory paths until end-to-end tests prove those paths are wired.
