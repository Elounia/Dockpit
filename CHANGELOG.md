# Changelog

## 1.1.0 — 2026-10-31

- Fix socket health classification, stopped-container inventory and CLI snapshot compatibility.
- Preserve raw TTY logs and bound socket/CLI log reads during collection.
- Escape external UI data, render logs as text, remove wildcard CORS and add browser security headers.
- Exclude healthcheck output from inspection and document socket authority and log sensitivity.
- Mark stale/missing measurements and keep the poller alive after daemon failures.
- Add health issue filtering, bounded one-hour host history, diagnostics and readiness.
- Add searchable, refreshable, downloadable logs with timestamps and pause.
- Remember filters, sorting and expanded stacks; support application labels and host-aware port links.
- Bundle offline frontend assets and correct wheel package discovery.
- Make socket group configuration explicit and fix systemd installation.
- Add socket/API/browser/Docker checks, multi-architecture release workflow and GitHub reporting templates.

## 1.0.0

Initial Docker stack and Linux host monitoring dashboard.
