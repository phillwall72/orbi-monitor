# Orbi Monitor

Monitors a Netgear Orbi mesh network (RBR760 router + satellites) running as a Docker container on Unraid. Provides a live web dashboard, ping charts, email alerts, and optional smart plug power-cycling for satellites.

---

## Features

- **Backhaul scraping** — Playwright headless Chromium logs into the Orbi admin UI every 10 minutes and reads backhaul status for each satellite
- **Ping monitoring** — continuous ping of each satellite IP every 60 seconds, with latency charted over 24 hours
- **Web dashboard** — live status cards, SVG ping charts, and Tapo/Kasa plug controls on port 8080; auto-refreshes every 60 seconds
- **Email alerts** — Gmail SMTP alerts for disconnections, status changes, ping timeouts, plug failures, and daily summaries
- **Smart plug integration** — optional power-cycle of satellites via TP-Link Tapo or Kasa plugs (see below)
- **SQLite history** — all ping results and backhaul status stored locally; data retained for 7 days, pruned nightly

---

## Data & Storage

All persistent data is stored in the `/data` volume mount:

| File | Purpose |
|------|---------|
| `orbi_monitor.db` | SQLite database — ping results, backhaul status, ping events |
| `satellite_state.json` | Last known state for each satellite (used for change detection) |
| `orbi_monitor.log` | Rolling log file |
| `tapo_config.json` | Smart plug configuration (auto-created on first run) |

**Retention:** ping results, backhaul status, and ping events are kept for **7 days** and pruned automatically at midnight each night.

---

## Smart Plug Support

Optional integration to power-cycle satellites when they go offline. Two plug types are supported:

### TP-Link Tapo (P100, P110)
Uses the local KLAP API. Requires your Tapo cloud account credentials.

```json
{
  "tapo_email": "your@email.com",
  "tapo_password": "your_tapo_password",
  "plugs": {
    "Satellite 1": { "ip": "192.168.0.x", "enabled": true, "plug_type": "tapo" },
    "Satellite 2": { "ip": "192.168.0.y", "enabled": true, "plug_type": "tapo" }
  }
}
```

### TP-Link Kasa (HS100, HS110)
Uses the local python-kasa protocol. No cloud credentials needed.

```json
{
  "plugs": {
    "Satellite 1": { "ip": "192.168.0.x", "enabled": true, "plug_type": "kasa" },
    "Satellite 2": { "ip": "192.168.0.y", "enabled": true, "plug_type": "kasa" }
  }
}
```

Edit `/data/tapo_config.json` on your Unraid server to configure plugs. The container picks up changes on the next check cycle without restarting.

---

## Installation on Unraid

### Option A — Pull from GitHub Container Registry (recommended)

1. In Unraid → Docker → Add Container
2. Set **Repository** to: `ghcr.io/phillwall72/orbi-monitor:latest`
3. Set **Extra Parameters** to: `--shm-size=1g`
4. Add the volume, port, and environment variables as below
5. Click **Apply**

### Option B — Import XML template

1. Copy `config/orbi-monitor.xml` to `/boot/config/plugins/dockerMan/templates-user/my-orbi-monitor.xml` on your Unraid server
2. In Unraid → Docker → Add Container → select **orbi-monitor** from your templates
3. Fill in the environment variables and click **Apply**

---

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `ORBI_IP` | `192.168.0.1` | Yes | IP address of your Orbi router |
| `ORBI_PASSWORD` | — | Yes | Orbi admin password |
| `ORBI_USER` | `admin` | No | Orbi admin username |
| `GMAIL_USER` | — | Yes | Gmail address to send alerts from |
| `GMAIL_APP_PASS` | — | Yes | Gmail App Password (16 characters) |
| `ALERT_TO` | — | Yes | Email address to receive alerts |
| `CHECK_INTERVAL_SECS` | `600` | No | Backhaul check interval in seconds |
| `PING_INTERVAL_SECS` | `60` | No | Ping interval in seconds |

---

## Ports & Volumes

| Type | Container | Host | Description |
|------|-----------|------|-------------|
| Port | `8080` | `8080` | Web dashboard |
| Volume | `/data` | `/mnt/user/appdata/orbi-monitor/data` | Persistent data |

### Changing the dashboard port

If port 8080 is already in use on your Unraid server, change the **host** port mapping. For example to use port 8090:

- In the Unraid Docker UI: Edit the container → change the **Host Port** from `8080` to `8090`
- Access the dashboard at `http://YOUR-UNRAID-IP:8090`

The container port (`8080`) stays the same — only the host side changes.

---

## Gmail App Password

Standard Gmail login passwords do not work. You need an App Password:

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Enable 2FA if not already done
3. Create an app password named "Orbi Monitor"
4. Use the 16-character code as `GMAIL_APP_PASS`

---

## Dashboard

Open `http://YOUR-UNRAID-IP:8080` in a browser.

- **Status cards** — current backhaul state and ping latency per satellite
- **Ping charts** — 24-hour SVG latency graphs per satellite
- **Plug controls** — toggle Tapo/Kasa plugs on/off from the dashboard
- **Auto-refresh** — page reloads every 60 seconds

---

## Releasing a New Version

Use the included release script from your Mac:

```bash
./release.sh          # patch bump (e.g. 3.5.18 → 3.5.19)
./release.sh minor    # minor bump (e.g. 3.5.x → 3.6.0)
./release.sh major    # major bump (e.g. 3.x.x → 4.0.0)
```

The script stages all changes, prompts for a commit message, tags the release, and pushes to GitHub. GitHub Actions builds and pushes the Docker image to `ghcr.io/phillwall72/orbi-monitor:latest` automatically. Then click **Force Update** in Unraid to deploy.

---

## Updating the Container (Unraid)

In the Unraid Docker tab, click the **orbi-monitor** container → **Force Update**. Unraid will pull the latest image and restart the container. Your data in `/data` is preserved.
