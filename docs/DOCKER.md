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

## GPU monitoring

The GPU card shows detected devices, utilization, dedicated VRAM, temperature and
power where available. NVIDIA uses `nvidia-smi`; AMD uses the host's DRM sysfs
counters through the existing read-only root mount. Intel device names are resolved
from the host PCI database when available. Current and configured maximum GPU clocks
are read from DRM sysfs, including per-GT paths. Integrated models identified by their
UHD/HD/Iris model name show **Shared system RAM**, rather than an unavailable dedicated
VRAM total. No arbitrary portion of host RAM is reported as GPU capacity.

Intel GPU load is currently not collected: the AMD `gpu_busy_percent` interface is
not provided by i915, and host performance counters need a separate Intel collector
and appropriate permissions. Clock frequency is not GPU utilization; a reading of
0 MHz must not be interpreted as a measured 0% load. GPU power and temperature are
shown only when exposed by the driver. Missing measurements remain null.

For NVIDIA in Docker, install NVIDIA Container Toolkit on the host, then use:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

This optional override uses a glibc-based Python image and requests NVIDIA's
utility capability for monitoring. The default Alpine image is unchanged.
Native installations use `nvidia-smi` from the host driver installation.

Container GPU statistics use NVIDIA process SM activity and framebuffer memory
from [`nvidia-smi pmon`](https://docs.nvidia.com/deploy/nvidia-smi/index.html#process-monitoring),
matched to Docker container IDs in host `/proc/<pid>/cgroup`. They work with both
the socket and CLI backends. Procfs must be readable; unsupported metrics, stopped
containers, failed process mapping, and non-NVIDIA container measurements display
`Unavailable`. Container GPU percent sums process activity: 100% represents one
GPU, and multiple GPUs can exceed 100%. Host GPU percent is the mean across devices.

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

### Frontend edits without rebuilding

The Compose service mounts `./dockpit/static` read-only at `/app/dockpit/static`.
HTML, JavaScript, compiled CSS, and local fonts are served from that directory.
Refresh the page after editing; the server reads HTML for each request and sends
`Cache-Control: no-store`. Run `scripts/build-css.sh` after changing `input.css`.
Python/backend changes still require rebuilding and restarting the service.
