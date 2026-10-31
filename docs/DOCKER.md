# Deployment & Integration Guide

Dockpit can be deployed as a Docker container, managed via Docker Compose / Portainer, or run directly as a systemd service.

---

## 1. Quick Run with Docker CLI

To launch Dockpit in a single command (bound safely to localhost):

```bash
docker run -d \
  --name dockpit \
  --restart unless-stopped \
  -p 127.0.0.1:8989:8989 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v /:/host/root:ro \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  dockpit:latest
```

Open `http://localhost:8989` in your web browser.

---

## 2. Docker Compose (Recommended)

Save the following as `docker-compose.yml`:

```yaml
services:
  dockpit:
    image: dockpit:latest
    container_name: dockpit
    restart: unless-stopped
    # Strictly bound to localhost for security
    ports:
      - "127.0.0.1:8989:8989"
    environment:
      - PYTHONUNBUFFERED=1
      - DOCKPIT_PORT=8989
      - DOCKPIT_HOST=0.0.0.0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro
    # Run with docker group permissions to access the docker socket
    group_add:
      - "${DOCKER_GID:-984}"
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp
```

Start the service:
```bash
docker compose up -d
```

---

## 3. Remote Access: Tailscale (Recommended) & Authelia SSO

By default, Dockpit binds strictly to `127.0.0.1:8989`. To access Dockpit remotely:

### 🥇 Recommended: Tailscale (Private Mesh VPN)
The recommended baseline for home labs and production servers is **Tailscale**:
- No ports opened on firewalls or routers.
- Direct authentication through your Google/GitHub/SSO account with mandatory 2FA.
- Instant HTTPS via `tailscale serve --bg 8989`.

### 🥈 Alternative: Reverse Proxy + Authelia SSO
If you need public domain web access without client software, place Dockpit behind a reverse proxy (Nginx, Caddy, Traefik) protected by **Authelia forward authentication**.

> [!TIP]
> For detailed configurations for both Tailscale and Authelia (including complete Docker Compose and Nginx `auth_request` examples), see **[AUTHELIA_SSO.md](AUTHELIA_SSO.md)**.

### Nginx Reverse Proxy Example
```nginx
server {
    listen 443 ssl http2;
    server_name dockpit.yourdomain.com;

    ssl_certificate /etc/ssl/certs/dockpit.crt;
    ssl_certificate_key /etc/ssl/private/dockpit.key;

    location / {
        proxy_pass http://127.0.0.1:8989;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Caddy Example
```caddy
dockpit.yourdomain.com {
    reverse_proxy 127.0.0.1:8989
}
```

---

## 4. Native Linux / Bare Metal Installation

If you prefer to run Dockpit directly on the host machine without Docker virtualization:

```bash
# Clone the repository
git clone https://github.com/Elounia/dockpit.git /opt/dockpit
cd /opt/dockpit

# Install systemd unit
sudo cp dockpit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dockpit.service

# Check service status
sudo systemctl status dockpit.service
```

