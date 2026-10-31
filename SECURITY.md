# Security policy

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/Elounia/dockpit/security/advisories/new). If the form is unavailable, contact the maintainer privately through their GitHub profile; do not open a public issue with exploit details, credentials or private logs.

Security fixes target the latest release. No response-time guarantee is provided.

## Trust boundary

Dockpit provides read-only HTTP endpoints, not a sandbox for Docker daemon access. Access to a rootful Docker socket grants root-equivalent capabilities. A read-only bind mount of that socket does not restrict API methods. Dropping capabilities and running as UID 1000 does not remove that authority.

Run on a trusted Linux host and restrict access using localhost, an SSH tunnel, a private VPN or an authenticated reverse proxy. There is no built-in authentication. Do not expose the API on an untrusted network. Configure MFA and access policy in your chosen identity provider; a VPN does not imply MFA is enabled.

The process can read host data through configured mounts. Inspection responses omit environment variables, command arguments and healthcheck output, but container logs, mount paths and network metadata may contain credentials. Do not share logs or downloads without reviewing them.

Browser assets are served locally. Third-party fonts and CSS are versioned and have their own licenses. No telemetry is implemented.

The API validates container identifiers against the current inventory. Log reads are bounded to 1 MiB and 500 requested lines; large snapshots can be truncated. These are resource limits, not a general denial-of-service defense. Apply connection limits at the reverse proxy where appropriate.
