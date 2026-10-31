# Deployment

## Docker Compose

From the repository root:

```bash
export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
docker compose up -d --build
docker compose ps
docker compose logs --tail=50
```

The port is published on `127.0.0.1:8989`. Use an SSH tunnel, VPN or authenticated reverse proxy for remote access. A bare Caddy/Nginx reverse proxy does not add authentication. See [AUTHELIA_SSO.md](AUTHELIA_SSO.md).

Read [SECURITY.md](../SECURITY.md) before mounting the Docker socket and host filesystem. `:ro` on a Unix socket does not restrict Docker API operations. The process runs as UID 1000 with the socket's host group added. This is not a rootless Docker daemon.

The image's healthcheck calls `/readyz`, which fails if no successful Docker snapshot exists or measurements are stale. `/healthz` checks HTTP liveness independently of Docker connectivity. Diagnostics in the dashboard reports the latest polling failure.

For an optional secondary disk, set a path visible inside Dockpit, for example `DOCKPIT_SECONDARY_STORAGE_PATH=/host/root/mnt/data`.

## Docker CLI

Use the complete build and run example in [README.md](../README.md#docker-cli). Both the local image build and `--group-add` are required. When release images exist, replace `dockpit:local` with a versioned GHCR image.

## Rootless Docker

Find the rootless daemon socket (usually `$XDG_RUNTIME_DIR/docker.sock`). Mount that path at `/var/run/docker.sock` inside Dockpit and derive `DOCKER_GID` from the actual socket. The service account must have access to the socket; subordinate UID/GID mappings can require additional configuration. Validate using Diagnostics. Host metrics and Docker Desktop VM metrics are not equivalent; the primary supported deployment is a native Linux Docker host.

## systemd

The checked-in unit runs a dedicated account from `/opt/dockpit`, so the package is importable without modifying the system Python installation:

```bash
sudo git clone https://github.com/Elounia/dockpit.git /opt/dockpit
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin dockpit
sudo cp /opt/dockpit/dockpit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dockpit.service
sudo systemctl status dockpit.service
```

The `docker` group must exist and match the socket's permissions. Docker group membership grants daemon privileges. Adjust the unit for rootless Docker. Keep the checkout owned by an administrator; the service account should not be able to modify its own executable code.

## Troubleshooting

- **Permission denied / no snapshots:** verify the socket's GID with `stat -c '%g'`, set `DOCKER_GID`, and recreate the container.
- **Healthy HTTP but not ready:** check `/api/diagnostics`; the daemon may be down or polls failing.
- **Stale data:** the latest successful measurement is too old. The banner intentionally retains previous values.
- **Missing host metrics:** verify procfs/root mounts and permissions. Missing data is not replaced with sample capacity.
- **Application links:** port links use the dashboard hostname and scheme. Behind a proxy/VPN, use `dockpit.url` for applications that are reached through their own domains; published ports may not be reachable through the dashboard's tunnel.
