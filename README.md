# Orbi Monitor v3

Monitors Orbi satellite backhaul status with:
- Playwright-based backhaul scraping every 10 minutes
- Continuous ping monitoring of satellite IPs every 60 seconds  
- Web dashboard on port 8080
- Email alerts for status changes, disconnections and errors

## Installation on Unraid

### Option A — Unraid Community Applications template (recommended)

1. Copy `orbi-monitor.xml` to `/boot/config/plugins/dockerMan/templates-user/` on your Unraid server
2. Copy `orbi-monitor-icon.svg` to `/boot/config/plugins/dockerMan/templates-user/` and convert to PNG (or use the SVG directly)
3. In Unraid → Docker → Add Container → select "orbi-monitor" from your templates
4. Fill in the environment variables and click Apply

### Option B — Manual build

```bash
mkdir -p /mnt/user/appdata/orbi-monitor/data
cd /mnt/user/appdata/orbi-monitor
# Copy monitor.py and Dockerfile here
docker build -t orbi-monitor .
docker run -d \
  --name orbi-monitor \
  --restart unless-stopped \
  --shm-size=1g \
  -p 8080:8080 \
  -v /mnt/user/appdata/orbi-monitor/data:/data \
  -e ORBI_IP=192.168.0.1 \
  -e ORBI_PASSWORD=your_password \
  -e ORBI_USER=admin \
  -e GMAIL_USER=youralerts@gmail.com \
  -e GMAIL_APP_PASS="xxxx xxxx xxxx xxxx" \
  -e ALERT_TO=you@icloud.com \
  -e CHECK_INTERVAL_SECS=600 \
  -e PING_INTERVAL_SECS=60 \
  -e DASHBOARD_PORT=8080 \
  orbi-monitor
```

## Dashboard

Open `http://YOUR-UNRAID-IP:8080` in a browser. Auto-refreshes every 60 seconds.

## Gmail App Password

1. Go to myaccount.google.com/apppasswords
2. Enable 2FA if not already done
3. Create app password named "Orbi Monitor"
4. Use the 16-character code in GMAIL_APP_PASS

## Updating environment variables (Unraid Docker UI)

Click the container name → Edit → change any variable → Apply.
The container restarts automatically with new settings.

## Removing the container cleanly

In Unraid Docker UI: click the container → Remove. The XML template
ensures Unraid tracks it properly — no orphan containers.
