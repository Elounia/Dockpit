# Changelog

## Unreleased — 2026-11-01

- Mount frontend assets read-only and serve HTML per request so edits appear on refresh without rebuilding.
- Show used/total RAM and equivalent logical CPUs alongside percentages in history tooltips.

- Redesign the overview with neutral surfaces, compact resource tiles and a single health status line.
- Show stacks as flat CPU/RAM rows; expand containers and open details or logs in a side drawer.
- Consolidate toolbar filters and merge freshness and diagnostics into an actionable system-status checklist.
- Report measurement reasons and fixes, poll duration, Dockpit CPU/RAM, hidden inventory and history capacity.
- Sample host CPU/RAM independently every second with a fixed 630-sample, tiered one-hour buffer and history deltas.
- Replace sparklines with a full-width canvas chart, monotone curves, range controls, and hover values.
- Limit chart animation to 300 ms per new sample and suspend client polling, log refresh and rendering in hidden tabs.
- Refresh the dashboard screenshot; retain offline assets, CSP, and zero runtime dependencies.

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
