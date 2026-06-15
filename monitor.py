#!/usr/bin/env python3
"""
Orbi Satellite Backhaul Monitor v3.4.0
- Playwright-based backhaul scraping every 10 minutes
- Enhanced ping burst monitoring (10 pings, min/max/avg/loss) every 60 seconds
- Tapo P110 smart plug integration via TP-Link cloud API (firmware 1.4.6 compatible)
- Per-satellite disconnect and ping timeout state machines with auto power cycle
- Web dashboard on port 8080 with SVG charts (responsive, log scale, 95th pct cap)
- Daily SVG summary email at midnight
- Version read main git check in tags
"""

import os, json, time, logging, smtplib, threading, subprocess, sqlite3, math, asyncio
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ─── Configuration ─────────────────────────────────────────────────────────────
ORBI_IP        = os.environ.get("ORBI_IP", "192.168.0.1")
ORBI_PASSWORD  = os.environ.get("ORBI_PASSWORD", "")
ORBI_USER      = os.environ.get("ORBI_USER", "admin")
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASS     = os.environ.get("GMAIL_APP_PASS", "")
ALERT_TO       = os.environ.get("ALERT_TO", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECS", "600"))
PING_INTERVAL  = int(os.environ.get("PING_INTERVAL_SECS", "60"))
DATA_DIR       = "/data"
DB_FILE        = f"{DATA_DIR}/orbi_monitor.db"
STATE_FILE     = f"{DATA_DIR}/satellite_state.json"
LOG_FILE       = f"{DATA_DIR}/orbi_monitor.log"
DASHBOARD_PORT  = 8080
TAPO_CONFIG     = f"{DATA_DIR}/tapo_config.json"

# ─── Backhaul score mapping ────────────────────────────────────────────────────
def backhaul_score(conn_type, backhaul_status):
    """Convert connection type + backhaul status to a 0-1 score."""
    status = backhaul_status.lower()
    is_5g  = "5g" in conn_type.lower() or conn_type == "5G"
    if "disconnect" in status or status == "": return 0.00
    if is_5g:
        return 1.00 if "good" in status else 0.75
    else:  # 2.4G
        return 0.50 if "good" in status else 0.25

SCORE_LABELS = {1.00: "5G Good", 0.75: "5G Poor", 0.50: "2.4G Good",
                0.25: "2.4G Poor", 0.00: "Disconnected"}
SCORE_COLORS = {1.00: "#2e7d32", 0.75: "#f9a825", 0.50: "#1565c0",
                0.25: "#e65100", 0.00: "#b71c1c"}

# ─── Logging — minimal ─────────────────────────────────────────────────────────
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)
# Only log INFO for our own module
log.setLevel(logging.INFO)

# ─── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ping_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        satellite_name TEXT NOT NULL,
        ip TEXT NOT NULL,
        latency_ms REAL,
        latency_min REAL,
        latency_max REAL,
        latency_avg REAL,
        packet_loss REAL,
        success INTEGER NOT NULL)""")
    # Migrate existing DB if columns missing
    try:
        c.execute("ALTER TABLE ping_results ADD COLUMN latency_min REAL")
        c.execute("ALTER TABLE ping_results ADD COLUMN latency_max REAL")
        c.execute("ALTER TABLE ping_results ADD COLUMN latency_avg REAL")
        c.execute("ALTER TABLE ping_results ADD COLUMN packet_loss REAL")
    except Exception:
        pass  # Columns already exist
    c.execute("""CREATE TABLE IF NOT EXISTS satellite_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        satellite_name TEXT NOT NULL,
        ip TEXT NOT NULL,
        backhaul_status TEXT,
        connection_type TEXT,
        connected_orbi TEXT,
        score REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ping_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        satellite_name TEXT NOT NULL,
        event_type TEXT NOT NULL,
        detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ping_time   ON ping_results(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_status_time ON satellite_status(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ping_events ON ping_events(timestamp)")
    conn.commit()
    conn.close()

def db_exec(sql, params=()):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

def db_fetch(sql, params=()):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def db_insert_ping(name, ip, latency_ms, success,
                   latency_min=None, latency_max=None,
                   latency_avg=None, packet_loss=None):
    db_exec(
        "INSERT INTO ping_results "
        "(timestamp,satellite_name,ip,latency_ms,latency_min,latency_max,"
        "latency_avg,packet_loss,success) VALUES (?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), name, ip, latency_ms,
         latency_min, latency_max, latency_avg, packet_loss,
         1 if success else 0)
    )

def db_insert_status(satellites):
    conn = sqlite3.connect(DB_FILE)
    for s in satellites:
        score = backhaul_score(s["connection_type"], s["backhaul_status"])
        conn.execute(
            "INSERT INTO satellite_status (timestamp,satellite_name,ip,backhaul_status,connection_type,connected_orbi,score) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), s["name"], s["ip"],
             s["backhaul_status"], s["connection_type"], s["connected_orbi"], score)
        )
    conn.commit()
    conn.close()

def db_get_ping_history(hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    return db_fetch(
        "SELECT timestamp,satellite_name,ip,latency_ms,latency_min,latency_max,"
        "latency_avg,packet_loss,success FROM ping_results WHERE timestamp>? ORDER BY timestamp",
        (since,)
    )

def db_get_status_history(hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    return db_fetch(
        "SELECT timestamp,satellite_name,backhaul_status,connection_type,score FROM satellite_status WHERE timestamp>? ORDER BY timestamp",
        (since,)
    )

def db_get_latest_status():
    return db_fetch("""
        SELECT s1.satellite_name,s1.ip,s1.backhaul_status,s1.connection_type,s1.connected_orbi,s1.timestamp,s1.score
        FROM satellite_status s1
        INNER JOIN (SELECT satellite_name,MAX(timestamp) as mt FROM satellite_status GROUP BY satellite_name) s2
        ON s1.satellite_name=s2.satellite_name AND s1.timestamp=s2.mt""")

def db_insert_ping_event(sat_name, event_type, detail=None):
    db_exec(
        "INSERT INTO ping_events (timestamp,satellite_name,event_type,detail) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), sat_name, event_type, detail)
    )

def db_get_ping_events(hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    return db_fetch(
        "SELECT timestamp,satellite_name,event_type,detail "
        "FROM ping_events WHERE timestamp>? ORDER BY timestamp",
        (since,)
    )

def db_cleanup():
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    db_exec("DELETE FROM ping_results    WHERE timestamp<?", (cutoff,))
    db_exec("DELETE FROM satellite_status WHERE timestamp<?", (cutoff,))
    db_exec("DELETE FROM ping_events     WHERE timestamp<?", (cutoff,))

# ─── SVG chart builder ─────────────────────────────────────────────────────────
def percentile(data, pct):
    """Return the pct-th percentile of a sorted list."""
    if not data:
        return 0
    s = sorted(data)
    k = (len(s) - 1) * pct / 100
    f, c = int(k), math.ceil(k)
    return s[f] if f == c else s[f] * (c - k) + s[c] * (k - f)


def log_scale_y(val, min_val, max_val, plot_h, ping_bot):
    """Map a latency value onto y using log scale. Returns y coordinate."""
    if val is None or val <= 0:
        return ping_bot
    log_min = math.log10(max(min_val, 1))
    log_max = math.log10(max(max_val, 2))
    if log_max <= log_min:
        return ping_bot
    frac = (math.log10(max(val, 1)) - log_min) / (log_max - log_min)
    frac = max(0.0, min(1.0, frac))
    return ping_bot - int(frac * plot_h)


def rolling_avg(data, window=5):
    """Simple rolling average over a list of (x, val) tuples."""
    result = []
    for i, (x, v) in enumerate(data):
        vals = [data[j][1] for j in range(max(0, i-window+1), i+1) if data[j][1] is not None]
        result.append((x, sum(vals)/len(vals) if vals else None))
    return result


def build_svg_chart(sat_name, hours=24, width=560, height=220):
    """
    Responsive SVG chart (uses viewBox so scales to any width).
    Layout:
      BACKHAUL label strip → backhaul score panel → divider
      PING (ms) label strip → ping latency panel → stats strip → x-axis
    Labels sit ABOVE their panels with no overlap.
    """
    VW = 560

    # Layout constants
    PAD_L   = 44   # left margin for y-axis labels
    PAD_R   = 12
    PAD_T   = 8    # top margin
    PAD_B   = 20   # bottom for x-axis
    LBL_H   = 14   # height of each label strip
    STATS_H = 12   # height of stats strip below ping
    GAP     = 6    # gap between panels

    plot_w  = VW - PAD_L - PAD_R

    # Compute available height for the two data panels
    fixed   = PAD_T + LBL_H + GAP + LBL_H + STATS_H + PAD_B
    panel_h = (height - fixed) // 2

    # Y positions
    bh_lbl_y  = PAD_T                           # backhaul label top
    bh_top    = bh_lbl_y + LBL_H               # backhaul data top
    bh_bot    = bh_top + panel_h               # backhaul data bottom
    div_y     = bh_bot + GAP // 2
    ping_lbl_y = bh_bot + GAP                  # ping label top
    ping_top  = ping_lbl_y + LBL_H            # ping data top
    ping_bot  = ping_top + panel_h            # ping data bottom
    stats_y   = ping_bot + 2                  # stats text y
    VH        = stats_y + STATS_H + PAD_B

    since   = (datetime.now() - timedelta(hours=hours)).isoformat()
    now     = datetime.now()
    t_start = now - timedelta(hours=hours)

    def time_to_x(ts_str):
        try:
            t    = datetime.fromisoformat(ts_str)
            frac = (t - t_start).total_seconds() / (hours * 3600)
            return PAD_L + max(0.0, min(1.0, frac)) * plot_w
        except Exception:
            return PAD_L

    status_rows = db_fetch(
        "SELECT timestamp,score FROM satellite_status "
        "WHERE satellite_name=? AND timestamp>? ORDER BY timestamp",
        (sat_name, since)
    )
    ping_rows = db_fetch(
        "SELECT timestamp,latency_ms,latency_min,latency_max,latency_avg,packet_loss,success "
        "FROM ping_results WHERE satellite_name=? AND timestamp>? ORDER BY timestamp",
        (sat_name, since)
    )

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VW} {VH}" '
        f'style="width:100%;max-width:{width}px;display:block;font-family:Arial,sans-serif;'
        f'background:#fff;border-radius:6px;">'
    ]

    # ── BACKHAUL label strip ──────────────────────────────────────────────────
    svg.append(
        f'<text x="{PAD_L}" y="{bh_lbl_y+10}" font-size="8" fill="#999" font-weight="bold" '
        f'letter-spacing="0.5">BACKHAUL</text>'
        f'<line x1="{PAD_L}" y1="{bh_top}" x2="{PAD_L+plot_w}" y2="{bh_top}" '
        f'stroke="#e8e8e8" stroke-width="0.5"/>'
    )

    # ── Backhaul score panel ──────────────────────────────────────────────────
    for label, val in [("1.0", 1.0), ("0.5", 0.5), ("0.0", 0.0)]:
        y = bh_top + panel_h - int(val * panel_h)
        svg.append(
            f'<line x1="{PAD_L}" y1="{y}" x2="{PAD_L+plot_w}" y2="{y}" '
            f'stroke="#e8e8e8" stroke-width="1"/>'
            f'<text x="{PAD_L-3}" y="{y+4}" text-anchor="end" font-size="8" fill="#bbb">{label}</text>'
        )

    score_bands = [
        (1.00, 0.875, "#2e7d3212"), (0.875, 0.625, "#f9a82512"),
        (0.625, 0.375, "#1565c012"), (0.375, 0.125, "#e6510012"),
        (0.125, 0.000, "#b71c1c12"),
    ]
    for hi, lo, col in score_bands:
        y1 = bh_top + panel_h - int(hi * panel_h)
        y2 = bh_top + panel_h - int(lo * panel_h)
        svg.append(f'<rect x="{PAD_L}" y="{y1}" width="{plot_w}" height="{max(1,y2-y1)}" fill="{col}"/>')

    if status_rows:
        pts = [(time_to_x(ts), bh_top + panel_h - int((s or 0)*panel_h), s or 0)
               for ts, s in status_rows]
        d = f"M {pts[0][0]:.1f} {pts[0][1]}"
        for i in range(1, len(pts)):
            d += f" H {pts[i][0]:.1f} V {pts[i][1]}"
        d += f" H {PAD_L+plot_w:.1f}"
        svg.append(f'<path d="{d}" fill="none" stroke="#1a237e" stroke-width="1.8"/>')
        for x, y, score in pts:
            col = SCORE_COLORS.get(score, "#888")
            svg.append(f'<circle cx="{x:.1f}" cy="{y}" r="2.5" fill="{col}" stroke="#fff" stroke-width="0.8"/>')
    else:
        svg.append(
            f'<text x="{PAD_L+plot_w//2}" y="{bh_top+panel_h//2}" '
            f'text-anchor="middle" font-size="9" fill="#ccc">No data</text>'
        )

    # ── Divider ───────────────────────────────────────────────────────────────
    svg.append(
        f'<line x1="{PAD_L}" y1="{div_y}" x2="{PAD_L+plot_w}" y2="{div_y}" '
        f'stroke="#e0e0e0" stroke-width="1" stroke-dasharray="4,3"/>'
    )

    # ── PING label strip ──────────────────────────────────────────────────────
    svg.append(
        f'<text x="{PAD_L}" y="{ping_lbl_y+10}" font-size="8" fill="#999" font-weight="bold" '
        f'letter-spacing="0.5">PING (ms)</text>'
        f'<line x1="{PAD_L}" y1="{ping_top}" x2="{PAD_L+plot_w}" y2="{ping_top}" '
        f'stroke="#e8e8e8" stroke-width="0.5"/>'
    )

    # ── Ping latency panel ────────────────────────────────────────────────────
    ping_h = ping_bot - ping_top

    valid_lats = sorted([r[4] for r in ping_rows if r[6] == 1 and r[4] is not None])

    if valid_lats:
        p95     = percentile(valid_lats, 95)
        cap     = max(p95, 10.0)
        min_lat = max(min(valid_lats), 1.0)

        log_min   = math.floor(math.log10(max(min_lat, 1)))
        log_max   = math.ceil(math.log10(max(cap, 2)))
        grid_vals = [10**e for e in range(int(log_min), int(log_max)+1) if 10**e <= cap*1.05]
        if not grid_vals or grid_vals[-1] < cap * 0.8:
            grid_vals.append(cap)

        for gv in grid_vals:
            gy    = log_scale_y(gv, min_lat, cap, ping_h, ping_bot)
            label = f"{int(gv)}" if gv >= 10 else f"{gv:.1f}"
            svg.append(
                f'<line x1="{PAD_L}" y1="{gy}" x2="{PAD_L+plot_w}" y2="{gy}" '
                f'stroke="#e8e8e8" stroke-width="1"/>'
                f'<text x="{PAD_L-3}" y="{gy+3}" text-anchor="end" font-size="8" fill="#bbb">{label}</text>'
            )

        avg_pts     = []
        min_pts     = []
        max_pts     = []
        outlier_pts = []
        timeout_xs  = []
        loss_pts    = []

        for row in ping_rows:
            ts_r   = row[0]
            lat_mn = row[2]
            lat_mx = row[3]
            lat_av = row[4]
            pkt_ls = row[5]
            succ   = row[6]
            x = time_to_x(ts_r)

            if not succ or lat_av is None:
                timeout_xs.append(x)
                loss_pts.append((x, 100.0))
                continue

            if pkt_ls is not None and pkt_ls > 0:
                loss_pts.append((x, pkt_ls))

            if lat_av > cap:
                outlier_pts.append(x)
                avg_pts.append((x, ping_top))
            else:
                avg_pts.append((x, log_scale_y(lat_av, min_lat, cap, ping_h, ping_bot)))

            if lat_mn is not None and lat_mn <= cap:
                min_pts.append((x, log_scale_y(lat_mn, min_lat, cap, ping_h, ping_bot)))
            if lat_mx is not None:
                mx_capped = min(lat_mx, cap)
                max_pts.append((x, log_scale_y(mx_capped, min_lat, cap, ping_h, ping_bot)))

        # Timeout zones — subtle grey shading (not heavy red blocks)
        # Group consecutive timeout x-coords into spans
        if timeout_xs:
            px_threshold = plot_w / max(len(ping_rows), 1) * 2
            spans = []
            span_start = timeout_xs[0]
            span_end   = timeout_xs[0]
            for x in timeout_xs[1:]:
                if x - span_end <= px_threshold:
                    span_end = x
                else:
                    spans.append((span_start, span_end))
                    span_start = span_end = x
            spans.append((span_start, span_end))
            for x1, x2 in spans:
                w = max(x2 - x1, 3)
                svg.append(
                    f'<rect x="{x1:.1f}" y="{ping_top}" width="{w:.1f}" height="{ping_h}" '
                    f'fill="#88888818" rx="1"/>'
                )

        # Min/max band
        if min_pts and max_pts and len(min_pts) == len(max_pts):
            band_top = " ".join(f"{'M' if i==0 else 'L'} {x:.1f} {y:.1f}"
                                for i,(x,y) in enumerate(max_pts))
            band_bot = " ".join(f"L {x:.1f} {y:.1f}"
                                for x,y in reversed(min_pts))
            svg.append(f'<path d="{band_top} {band_bot} Z" fill="#1565c015"/>')

        # Avg line
        if avg_pts:
            d_avg = " ".join(f"{'M' if i==0 else 'L'} {x:.1f} {y:.1f}"
                             for i,(x,y) in enumerate(avg_pts))
            svg.append(f'<path d="{d_avg}" fill="none" stroke="#1565c0" stroke-width="1.8"/>')

        # Outlier dots
        for x in outlier_pts:
            svg.append(
                f'<circle cx="{x:.1f}" cy="{ping_top+3}" r="2.5" '
                f'fill="#e65100" stroke="#fff" stroke-width="0.8"/>'
            )

        # Packet loss dots along bottom
        for x, loss in loss_pts:
            if loss > 0:
                r_size = max(1.5, min(4.0, loss / 25.0 * 4.0))
                svg.append(
                    f'<circle cx="{x:.1f}" cy="{ping_bot-3}" r="{r_size:.1f}" '
                    f'fill="#b71c1c55"/>'
                )

        # ── Stats strip — sits BELOW ping panel, right-aligned ───────────────
        overall_avg = sum(valid_lats) / len(valid_lats)
        losses      = [r[5] for r in ping_rows if r[5] is not None]
        avg_loss    = sum(losses)/len(losses) if losses else 0
        svg.append(
            f'<text x="{PAD_L+plot_w}" y="{stats_y+9}" text-anchor="end" '
            f'font-size="7.5" fill="#999">'
            f'avg {overall_avg:.1f}ms · loss {avg_loss:.1f}% · 95th={int(cap)}ms'
            f'</text>'
        )

    else:
        svg.append(
            f'<text x="{PAD_L+plot_w//2}" y="{ping_top+ping_h//2}" '
            f'text-anchor="middle" font-size="9" fill="#ccc">No ping data</text>'
        )

    # ── X-axis labels ─────────────────────────────────────────────────────────
    x_axis_y = VH - 4
    intervals = [0, 6, 12, 18, 24] if hours >= 24 else [0, hours//4, hours//2, 3*hours//4, hours]
    for h_off in intervals:
        if h_off > hours: continue
        t_lbl = t_start + timedelta(hours=h_off)
        x     = PAD_L + (h_off / hours) * plot_w
        svg.append(
            f'<text x="{x:.1f}" y="{x_axis_y}" text-anchor="middle" font-size="8" fill="#bbb">'
            f'{t_lbl.strftime("%H:%M")}</text>'
        )

    svg.append('</svg>')
    return "\n".join(svg)


def build_score_legend():
    """Small SVG legend for score values."""
    items = [
        (1.00, "5G Good"),
        (0.75, "5G Poor"),
        (0.50, "2.4G Good"),
        (0.25, "2.4G Poor"),
        (0.00, "Disconnected"),
    ]
    w, h = 340, 24
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    x = 0
    for score, label in items:
        col = SCORE_COLORS[score]
        svg.append(f'<circle cx="{x+6}" cy="12" r="5" fill="{col}"/>')
        svg.append(f'<text x="{x+14}" y="16" font-size="10" fill="#555" font-family="Arial">{label}</text>')
        x += 68
    svg.append('</svg>')
    return "\n".join(svg)


# ─── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject, body_text, html_body=None, high_priority=False, is_error=False):
    if not GMAIL_USER or not GMAIL_PASS or not ALERT_TO:
        log.error("Email not configured")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Orbi Monitor <{GMAIL_USER}>"
    msg["To"]      = ALERT_TO
    if high_priority or is_error:
        msg["X-Priority"] = "1"
        msg["Importance"]  = "High"
    msg.attach(MIMEText(body_text, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, ALERT_TO, msg.as_string())
        log.info(f"Email: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def alert_email(subject, body, high_priority=False, is_error=False):
    """Send a plain alert email (no charts)."""
    if is_error:
        colour, icon = "#F57F17", "🔧"
    elif high_priority:
        colour, icon = "#b71c1c", "🚨"
    else:
        colour, icon = "#1565c0", "⚠️"

    html = f"""<html><body>
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;">
      <div style="background:{colour};color:white;padding:12px 20px;border-radius:6px 6px 0 0;">
        <h2 style="margin:0;">{icon} Orbi Monitor</h2>
      </div>
      <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 6px 6px;">
        <pre style="white-space:pre-wrap;font-family:Arial,sans-serif;">{body}</pre>
        <p style="color:#aaa;font-size:11px;margin-top:16px;">{datetime.now().strftime('%d %b %Y %H:%M')}</p>
      </div>
    </div></body></html>"""
    send_email(subject, body, html_body=html, high_priority=high_priority, is_error=is_error)


def send_daily_summary():
    """Build and send the daily summary with SVG charts."""
    log.info("Daily summary: starting")
    now = datetime.now()

    # Get all known satellites from last 24h
    log.info("Daily summary: querying satellite list")
    rows = db_fetch(
        "SELECT satellite_name, ip FROM satellite_status "
        "WHERE timestamp>? GROUP BY satellite_name "
        "ORDER BY satellite_name",
        ((now - timedelta(hours=24)).isoformat(),)
    )
    if not rows:
        alert_email("Orbi Monitor: Daily Summary", "No satellite data recorded in the last 24 hours.", is_error=True)
        return
    log.info(f"Daily summary: found {len(rows)} satellites")

    # Build stats per satellite
    stats = {}
    for sat_name, ip in rows:
        log.info(f"Daily summary: processing {sat_name} ({ip})")
        since = (now - timedelta(hours=24)).isoformat()
        pings = db_fetch(
            "SELECT success,latency_avg,packet_loss FROM ping_results "
            "WHERE satellite_name=? AND timestamp>?",
            (sat_name, since)
        )
        statuses = db_fetch(
            "SELECT backhaul_status,connection_type,score FROM satellite_status WHERE satellite_name=? AND timestamp>? ORDER BY timestamp",
            (sat_name, since)
        )
        total_pings  = len(pings)
        ok_pings     = sum(1 for p in pings if p[0])
        latencies    = [p[1] for p in pings if p[0] and p[1] is not None]
        avg_lat      = sum(latencies)/len(latencies) if latencies else None
        uptime_pct   = 100*ok_pings/total_pings if total_pings else 0
        loss_vals    = [p[2] for p in pings if p[2] is not None]
        avg_loss_pct = sum(loss_vals)/len(loss_vals) if loss_vals else 0
        avg_score   = sum(s[2] for s in statuses if s[2] is not None)/len(statuses) if statuses else 0
        # Count transitions
        transitions = sum(1 for i in range(1, len(statuses))
                         if statuses[i][2] != statuses[i-1][2])
        current_status = statuses[-1] if statuses else ("Unknown", "Unknown", 0)
        ping_evts     = db_fetch(
            "SELECT event_type FROM ping_events WHERE satellite_name=? AND timestamp>?",
            (sat_name, since)
        )
        timeout_count = sum(1 for e in ping_evts if e[0] == "TIMEOUT_CONFIRMED")
        cycle_count   = sum(1 for e in ping_evts if e[0] == "POWER_CYCLED")
        stats[sat_name] = {
            "ip":             ip,
            "uptime_pct":     uptime_pct,
            "avg_lat":        avg_lat,
            "avg_loss_pct":   avg_loss_pct,
            "avg_score":      avg_score,
            "transitions":    transitions,
            "current_status": current_status[0],
            "current_type":   current_status[1],
            "timeout_count":  timeout_count,
            "cycle_count":    cycle_count,
        }

    # Build HTML email
    date_str = now.strftime("%d %b %Y")
    html_parts = [f"""<html><body>
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#1a237e;color:white;padding:14px 20px;border-radius:6px 6px 0 0;">
        <h2 style="margin:0;">📊 Orbi Daily Summary — {date_str}</h2>
      </div>
      <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 6px 6px;">"""]

    # Summary table
    html_parts.append("""
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:13px;">
          <tr style="background:#f5f5f5;">
            <th style="padding:6px 8px;text-align:left;border:1px solid #e0e0e0;">Satellite</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Current Status</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Ping Uptime</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Avg Latency</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Avg Loss</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Avg Score</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Changes</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Ping Timeouts</th>
            <th style="padding:6px 8px;text-align:center;border:1px solid #e0e0e0;">Power Cycles</th>
          </tr>""")

    for sat_name, s in stats.items():
        score_col = SCORE_COLORS.get(round(s["avg_score"]*4)/4, "#888")
        lat_str      = f"{s['avg_lat']:.1f}ms" if s['avg_lat'] else "n/a"
        loss_str     = f"{s['avg_loss_pct']:.1f}%" if s['avg_loss_pct'] is not None else "n/a"
        status_label = f"{s['current_type']} {s['current_status']}"
        html_parts.append(f"""
          <tr>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;font-weight:bold;">{sat_name}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{status_label}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{s['uptime_pct']:.1f}%</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{lat_str}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{loss_str}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;color:{score_col};font-weight:bold;">{s['avg_score']:.2f}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{s['transitions']}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{s['timeout_count']}</td>
            <td style="padding:6px 8px;border:1px solid #e0e0e0;text-align:center;">{s['cycle_count']}</td>
          </tr>""")

    html_parts.append("</table>")

    # Legend
    html_parts.append(f"""
        <p style="font-size:11px;color:#666;margin-bottom:8px;">Score: 1.0=5G Good · 0.75=5G Poor · 0.5=2.4G Good · 0.25=2.4G Poor · 0=Disconnected</p>
        {build_score_legend()}
        <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">""")

    # SVG charts per satellite
    log.info("Daily summary: building SVG charts")
    for sat_name in stats:
        svg = build_svg_chart(sat_name, hours=24, width=560, height=160)
        html_parts.append(f"""
        <p style="font-size:13px;font-weight:bold;color:#333;margin:16px 0 6px;">{sat_name}</p>
        {svg}""")

    html_parts.append(f"""
        <p style="color:#aaa;font-size:11px;margin-top:20px;">
          Generated {now.strftime('%d %b %Y %H:%M')} · Orbi Monitor
        </p>
      </div>
    </div></body></html>""")

    html = "\n".join(html_parts)

    # Plain text version
    plain = f"Orbi Daily Summary — {date_str}\n\n"
    for sat_name, s in stats.items():
        lat_str  = f"{s['avg_lat']:.1f}ms"      if s['avg_lat']      else "n/a"
        loss_str = f"{s['avg_loss_pct']:.1f}%"  if s['avg_loss_pct'] is not None else "n/a"
        plain += (f"{sat_name} ({s['ip']})\n"
                  f"  Status   : {s['current_type']} {s['current_status']}\n"
                  f"  Uptime   : {s['uptime_pct']:.1f}%\n"
                  f"  Avg ping : {lat_str}\n"
                  f"  Avg loss : {loss_str}\n"
                  f"  Avg score: {s['avg_score']:.2f}\n"
                  f"  Changes  : {s['transitions']}\n"
                  f"  Ping timeouts: {s['timeout_count']}\n"
                  f"  Power cycles : {s['cycle_count']}\n\n")

    log.info("Daily summary: sending email")
    send_email(f"📊 Orbi Daily Summary — {date_str}", plain, html_body=html)
    log.info("Daily summary: complete")



# ─── Tapo config & state ───────────────────────────────────────────────────────

_tapo_config      = {}          # loaded from TAPO_CONFIG file
_tapo_plug_status = {}          # {sat_name: {"reachable": bool, "power_w": float, "today_kwh": float, ...}}
_plug_reach_state = {}          # {sat_name: None | "UNREACHABLE_1" | "UNREACHABLE_CONFIRMED" | "WAITING_MANUAL"}
_plug_reach_state = {}          # {sat_name: None | "UNREACHABLE_1" | "WAITING_MANUAL"}
_tapo_lock        = threading.Lock()

# Per-satellite disconnect state machine
# States: None | "DISCONNECTED_1" | "DISCONNECTED_CONFIRMED" | "WAITING_PLUG" |
#         "POWER_CYCLED" | "POWER_CYCLED_CHECK_1" | "POWER_CYCLED_CHECK_2" |
#         "POWER_ON_PENDING" | "WAITING_MANUAL"
_disconnect_state = {}          # {sat_name: state_string}


def load_tapo_config():
    """Load tapo_config.json, create template if missing."""
    global _tapo_config
    if not os.path.exists(TAPO_CONFIG):
        template = {
            "tapo_email":    "CONFIGURE_ME",
            "tapo_password": "CONFIGURE_ME",
            "plugs": {}
        }
        with open(TAPO_CONFIG, "w") as f:
            json.dump(template, f, indent=2)
        log.info("Created Tapo config template — edit /data/tapo_config.json to configure")
    with open(TAPO_CONFIG) as f:
        _tapo_config = json.load(f)


def save_tapo_config():
    """Persist current _tapo_config to disk."""
    with open(TAPO_CONFIG, "w") as f:
        json.dump(_tapo_config, f, indent=2)


def ensure_satellite_in_config(sat_name):
    """Add a placeholder plug entry for a newly discovered satellite."""
    if sat_name not in _tapo_config.get("plugs", {}):
        _tapo_config.setdefault("plugs", {})[sat_name] = {
            "ip":               "CONFIGURE_ME",
            "plug_type":        "tapo",
            "enabled":          False,
            "grace_period_secs": 300,
            "auto_discovered":  True,
            "last_seen":        "never"
        }
        save_tapo_config()
        log.info(f"Added placeholder Tapo config for {sat_name}")


def is_plug_configured(sat_name):
    """True if plug has a real IP and is enabled."""
    plug = _tapo_config.get("plugs", {}).get(sat_name, {})
    return (plug.get("enabled", False) and
            plug.get("ip", "CONFIGURE_ME") not in ("CONFIGURE_ME", "", None))


def tapo_credentials_ok():
    """True if email/password look configured."""
    return (_tapo_config.get("tapo_email",    "CONFIGURE_ME") != "CONFIGURE_ME" and
            _tapo_config.get("tapo_password",  "CONFIGURE_ME") != "CONFIGURE_ME")


def _run_async(coro):
    """Run an async coroutine synchronously (safe from any thread)."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # If there's already a running loop (shouldn't happen in our threads
        # but just in case), run in a new thread with its own event loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)


async def _kasa_get_client(ip):
    """Create a python-kasa IotPlug client for Kasa devices (HS100 etc).
    No credentials needed — Kasa uses local protocol without auth.
    """
    from kasa.iot import IotPlug
    p = IotPlug(ip)
    await p.update()
    return p


async def _tapo_get_client(ip):
    """Create a local tapo ApiClient connected to a specific plug IP.
    Tries P110 first (energy monitoring), falls back to P100/HS100 if that fails.
    Pre-checks ICMP reachability to avoid hanging on unresponsive devices.
    """
    if not probe_plug(ip):
        raise Exception(f"Plug at {ip} not reachable (ping failed)")
    from tapo import ApiClient
    client = ApiClient(
        _tapo_config["tapo_email"],
        _tapo_config["tapo_password"]
    )
    try:
        return await client.p110(ip)
    except Exception:
        # Fall back to generic plug (HS100, P100, etc) — no energy monitoring
        return await client.p100(ip)


async def _get_plug_client(sat_name):
    """Get the right client based on plug_type in config.
    tapo → KLAP local API (P110, P100)
    kasa → python-kasa local API (HS100, HS110 etc)
    """
    plug = _tapo_config["plugs"][sat_name]
    ip   = plug["ip"]
    plug_type = plug.get("plug_type", "tapo").lower()
    if plug_type == "kasa":
        return "kasa", await _kasa_get_client(ip)
    else:
        return "tapo", await _tapo_get_client(ip)


async def _tapo_check_login_async():
    """Validate Tapo credentials by connecting to any available Tapo plug.
    Skips Kasa devices (no credentials needed for those).
    Succeeds if any one Tapo device responds.
    """
    errors = []
    for sat_name, plug in _tapo_config.get("plugs", {}).items():
        ip        = plug.get("ip", "CONFIGURE_ME")
        plug_type = plug.get("plug_type", "tapo").lower()
        if not plug.get("enabled") or ip in ("CONFIGURE_ME", "", None):
            continue
        if plug_type == "kasa":
            continue  # Kasa devices don't need Tapo credentials
        try:
            plug_type_out, device = await _get_plug_client(sat_name)
            info = await device.get_device_info()
            return True, f"Connected to {sat_name} ({ip}) — on={info.device_on}"
        except Exception as e:
            errors.append(f"{sat_name}: {e}")
            continue
    if errors:
        return False, "; ".join(errors)
    return True, "No Tapo plugs configured — credentials not validated"


async def _tapo_get_device_info_async(sat_name, include_energy=False):
    """Fetch device state via local API — dispatches to Tapo or Kasa based on plug_type."""
    plug_type, device = await _get_plug_client(sat_name)

    if plug_type == "kasa":
        # python-kasa: update() already called in _kasa_get_client
        result = {
            "reachable":   True,
            "power_on":    device.is_on,
            "power_w":     None,
            "today_kwh":   None,
            "signal_rssi": None,
            "nickname":    device.alias or sat_name,
        }
        if include_energy and device.has_emeter:
            try:
                emeter = device.emeter_realtime
                result["power_w"]   = float(emeter.get("power", 0))
                result["today_kwh"] = float(device.emeter_today or 0) / 1000.0
            except Exception as e:
                log.info(f"KASA {sat_name}: energy fetch failed: {e}")
        return result
    else:
        # tapo library
        info   = await device.get_device_info()
        result = {
            "reachable":   True,
            "power_on":    info.device_on,
            "power_w":     None,
            "today_kwh":   None,
            "signal_rssi": info.rssi,
            "nickname":    info.nickname if info.nickname else sat_name,
        }
        if include_energy:
            try:
                usage         = await device.get_energy_usage()
                current       = await device.get_current_power()
                result["power_w"]   = float(current.current_power)
                result["today_kwh"] = usage.today_energy / 1000.0
            except AttributeError:
                pass  # No energy monitoring on this device
            except Exception as e:
                log.warning(f"TAPO {sat_name}: energy fetch failed: {e}")
        return result


async def _tapo_turn_on_with_retry(sat_name, retries=5, delay=15):
    """Attempt to turn on a plug with retries.
    Used after power-off when plug may temporarily lose wifi
    (e.g. connected via the satellite being rebooted).
    Returns True if succeeded, False if all retries exhausted.
    """
    for attempt in range(retries):
        try:
            _, device = await _get_plug_client(sat_name)
            if hasattr(device, "on"):
                await device.on()
            else:
                await device.turn_on()
            log.info(f"PLUG {sat_name}: turned on (attempt {attempt+1})")
            return True
        except Exception as e:
            log.info(f"PLUG {sat_name}: turn on attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    return False


async def _tapo_power_cycle_async(sat_name):
    """Power off then on — dispatches to Tapo (KLAP) or Kasa based on plug_type.
    Retries turn-on in case plug loses wifi during the off period
    (e.g. when plug is connected via the satellite being rebooted).
    Raises PowerOnFailed if all retries exhausted — caller sets POWER_ON_PENDING.
    """
    plug_type, device = await _get_plug_client(sat_name)
    # Turn off
    if plug_type == "kasa":
        await device.turn_off()
    else:
        await device.off()
    await asyncio.sleep(10)
    # Turn on with retry — plug may have lost wifi during off period
    success = await _tapo_turn_on_with_retry(sat_name, retries=5, delay=15)
    if not success:
        raise Exception("POWER_ON_FAILED")


def check_tapo_login():
    """Synchronous wrapper — check Tapo login, send alert if broken."""
    if not tapo_credentials_ok():
        log.info("Tapo: credentials not configured — skipping login check")
        return False
    ok, err = _run_async(_tapo_check_login_async())
    if not ok:
        log.warning(f"Tapo login failed: {err}")
        ts  = datetime.now().strftime("%d %b %Y %H:%M")
        msg = (f"Could not authenticate with the Tapo API.\n\n"
               f"Error: {err}\n\n"
               f"Check tapo_email and tapo_password in /data/tapo_config.json\n"
               f"Plug power cycling will be unavailable until fixed.")
        alert_email("🔧 Orbi Monitor: Tapo Login Failed", msg, is_error=True)
        return False
    log.info("Tapo login OK")
    return True


def probe_plug(ip, timeout=3):
    """Check plug reachability using ICMP ping.
    TCP probe on ports 80/9999 fails on Tapo firmware 1.4.6+."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True, timeout=timeout + 2
        )
        return result.returncode == 0
    except Exception:
        return False


def _handle_plug_reachability(sat_name, ip, reachable):
    """Drive plug reachability state machine for enabled plugs only."""
    current = _plug_reach_state.get(sat_name)
    now_str = datetime.now().strftime("%d %b %Y %H:%M")

    if reachable:
        if current in ("UNREACHABLE_CONFIRMED", "WAITING_MANUAL"):
            log.info(f"PLUG {sat_name}: reachable again (was {current})")
            body = (f"Tapo plug for {sat_name} ({ip})\nTime: {now_str}\n\n"
                    "Plug is reachable again.")
            alert_email(f"\u2705 Orbi: {sat_name} Tapo plug reachable again", body)
        _plug_reach_state[sat_name] = None
        return

    if current is None:
        _plug_reach_state[sat_name] = "UNREACHABLE_1"
        log.info(f"PLUG {sat_name}: unreachable (1st)")
    elif current == "UNREACHABLE_1":
        _plug_reach_state[sat_name] = "WAITING_MANUAL"
        log.info(f"PLUG {sat_name}: unreachable confirmed")
        body = (f"Tapo plug for {sat_name} ({ip})\nTime: {now_str}\n\n"
                "Plug unreachable for 2 consecutive checks.\n"
                "Auto power cycling unavailable until resolved.\n\n"
                "Check the plug is powered and on WiFi.")
        alert_email(
            f"\U0001f527 Orbi: {sat_name} Tapo plug unreachable",
            body, is_error=True)
    # WAITING_MANUAL: no repeat alerts until plug recovers


def update_plug_statuses(satellites):
    """
    Called on every backhaul check cycle.
    Updates _tapo_plug_status for each configured satellite plug.
    """
    with _tapo_lock:
        # Reload config on every cycle so edits take effect without restart
        load_tapo_config()
        for sat in satellites:
            name = sat["name"]
            ensure_satellite_in_config(name)
            if not is_plug_configured(name):
                _tapo_plug_status[name] = {"reachable": None, "enabled": False}
                _plug_reach_state[name] = None
                continue

            ip        = _tapo_config["plugs"][name]["ip"]
            reachable = probe_plug(ip)

            # Drive reachability alert state machine
            _handle_plug_reachability(name, ip, reachable)

            if reachable and tapo_credentials_ok():
                try:
                    info = _run_async(
                        _tapo_get_device_info_async(name, include_energy=True)
                    )
                    info["reachable"] = True
                    _tapo_plug_status[name] = info
                except Exception as e:
                    log.warning(f"Tapo read failed for {name}: {e}")
                    _tapo_plug_status[name] = {"reachable": True, "error": str(e)}
            else:
                _tapo_plug_status[name] = {"reachable": reachable, "enabled": True}


def power_cycle_plug(sat_name):
    """
    Attempt to power cycle the plug for sat_name.
    Returns (attempted: bool, success: bool, error: str|None)
    If error is POWER_ON_FAILED the plug was turned off but on() failed —
    caller should enter POWER_ON_PENDING state.
    """
    if not is_plug_configured(sat_name):
        return False, False, "No plug configured"
    ip = _tapo_config["plugs"][sat_name]["ip"]
    if not probe_plug(ip):
        return True, False, f"Plug unreachable at {ip}"
    try:
        _run_async(_tapo_power_cycle_async(sat_name))
        return True, True, None
    except Exception as e:
        return True, False, str(e)  # POWER_ON_FAILED if turn-on retries exhausted


def handle_disconnect_state(sat_name, sat, state):
    """
    Drive the per-satellite disconnect state machine.
    Called when backhaul_status == "Disconnected".
    Sends appropriate alerts and triggers power cycle when conditions met.
    """
    current = _disconnect_state.get(sat_name)
    now_str = datetime.now().strftime("%d %b %Y %H:%M")

    if current is None:
        # First disconnect detection
        _disconnect_state[sat_name] = "DISCONNECTED_1"
        log.info(f"STATUS {sat_name}: DISCONNECTED (1st detection)")

    elif current == "DISCONNECTED_1":
        # Second consecutive disconnect — confirmed
        _disconnect_state[sat_name] = "DISCONNECTED_CONFIRMED"
        log.info(f"STATUS {sat_name}: DISCONNECTED confirmed")

        plug_status = _tapo_plug_status.get(sat_name, {})
        reachable   = plug_status.get("reachable")
        configured  = is_plug_configured(sat_name)

        if not configured:
            alert_email(
                f"🚨 ORBI ALERT: {sat_name} DISCONNECTED",
                (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
                 f"❌ Backhaul lost — no Tapo plug configured for auto-recovery.\n"
                 f"Manual intervention required."),
                high_priority=True
            )
        elif not reachable:
            alert_email(
                f"🔧 Orbi: {sat_name} disconnected — waiting for plug to come back",
                (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
                 f"❌ Backhaul lost.\n"
                 f"⚠️  Tapo plug is currently unreachable.\n"
                 f"Will power cycle automatically when plug comes back online."),
                is_error=True
            )
            _disconnect_state[sat_name] = "WAITING_PLUG"
        else:
            # Plug reachable — attempt power cycle
            attempted, success, err = power_cycle_plug(sat_name)
            if success:
                alert_email(
                    f"⚡ Orbi: {sat_name} power cycled",
                    (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
                     f"❌ Backhaul disconnected — Tapo plug power cycled.\n"
                     f"Waiting for satellite to recover...\n\n"
                     f"A follow-up alert will be sent in ~{CHECK_INTERVAL*2//60} minutes "
                     f"if it has not reconnected.")
                )
                _disconnect_state[sat_name] = "POWER_CYCLED"
                log.info(f"STATUS {sat_name}: power cycled")
            elif err == "POWER_ON_FAILED":
                # Plug was turned off but on() failed — wait for plug to reconnect
                alert_email(
                    f"🔧 Orbi: {sat_name} — plug off, waiting to turn back on",
                    (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
                     f"Plug was turned off but could not be turned back on.\n"
                     f"This can happen when the plug connects via the satellite being rebooted.\n"
                     f"Will turn on automatically when plug reconnects to network."),
                    is_error=True
                )
                _disconnect_state[sat_name] = "POWER_ON_PENDING"
                log.info(f"STATUS {sat_name}: POWER_ON_PENDING — waiting for plug to reconnect")
            else:
                alert_email(
                    f"🚨 ORBI ALERT: {sat_name} DISCONNECTED — power cycle failed",
                    (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
                     f"❌ Backhaul lost. Power cycle attempted but failed.\n"
                     f"Error: {err}\n\nManual intervention required."),
                    high_priority=True
                )
                _disconnect_state[sat_name] = "WAITING_MANUAL"

    elif current == "POWER_CYCLED":
        _disconnect_state[sat_name] = "POWER_CYCLED_CHECK_1"
        log.info(f"STATUS {sat_name}: still disconnected after power cycle (check 1)")

    elif current == "POWER_CYCLED_CHECK_1":
        _disconnect_state[sat_name] = "POWER_CYCLED_CHECK_2"
        log.info(f"STATUS {sat_name}: still disconnected after power cycle (check 2)")

    elif current == "POWER_CYCLED_CHECK_2":
        # Power cycle has not recovered — escalate
        alert_email(
            f"🚨 ORBI ALERT: {sat_name} — power cycle failed to recover",
            (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
             f"❌ Satellite remains disconnected after power cycle.\n"
             f"Manual intervention required."),
            high_priority=True
        )
        _disconnect_state[sat_name] = "WAITING_MANUAL"
        log.info(f"STATUS {sat_name}: power cycle failed to recover — WAITING_MANUAL")

    elif current == "WAITING_MANUAL":
        # Already alerted — no further action
        pass


def handle_reconnect(sat_name, sat, score):
    """Called when a satellite reconnects from any disconnected state.
    If we were WAITING_PLUG the satellite recovered on its own — no power cycle needed.
    """
    prev_state = _disconnect_state.get(sat_name)
    _disconnect_state[sat_name] = None  # Full reset
    now_str = datetime.now().strftime("%d %b %Y %H:%M")
    log.info(f"STATUS {sat_name}: reconnected (was {prev_state}) — score {score:.2f}")
    alert_email(
        f"✅ Orbi: {sat_name} reconnected",
        (f"Satellite: {sat_name} ({sat['ip']})\nTime: {now_str}\n\n"
         f"✅ Backhaul restored — score {score:.2f} "
         f"({SCORE_LABELS.get(score, '')})\n\n"
         f"Previous state: {prev_state or 'unknown'}")
    )


# ─── Ping timeout state machine ────────────────────────────────────────────────
# States in _disconnect_state with PING_ prefix:
# None | PING_TIMEOUT_1 | PING_TIMEOUT_CONFIRMED | PING_POWER_CYCLED |
# PING_POWER_CYCLED_CHECK_1 | PING_POWER_CYCLED_CHECK_2 | PING_WAITING_MANUAL

def _ping_state(n):        return _disconnect_state.get(f"PING_{n}")
def _set_ping_state(n, s): _disconnect_state[f"PING_{n}"] = s

def _backhaul_already_cycling(sat_name):
    return _disconnect_state.get(sat_name) in (
        "POWER_CYCLED","POWER_CYCLED_CHECK_1","POWER_CYCLED_CHECK_2")


def handle_ping_timeout(sat_name, sat_ip):
    """Drive ping timeout state machine. Called by ping_worker on 100% packet loss.
    Auto power cycle from ping is DISABLED — alert only so we can track whether
    ping timeouts lead to backhaul disconnections. Power cycle only happens via
    the backhaul disconnect state machine.
    """
    current = _ping_state(sat_name)
    now_str = datetime.now().strftime("%d %b %Y %H:%M")

    if current is None:
        _set_ping_state(sat_name, "PING_TIMEOUT_1")
        log.info(f"PING {sat_name}: timeout (1st)")
        db_insert_ping_event(sat_name, "TIMEOUT_1")
        return

    if current == "PING_TIMEOUT_1":
        _set_ping_state(sat_name, "PING_TIMEOUT_CONFIRMED")
        log.info(f"PING {sat_name}: timeout confirmed")
        db_insert_ping_event(sat_name, "TIMEOUT_CONFIRMED")
        bh_cycle = _backhaul_already_cycling(sat_name)

        if bh_cycle:
            body = (f"Satellite: {sat_name} ({sat_ip})\nTime: {now_str}\n\n"
                    "Ping not responding (100% packet loss).\n"
                    "A power cycle is already in progress via backhaul monitor.\n"
                    "Waiting for recovery...")
            alert_email(f"\U0001f527 Orbi: {sat_name} ping timeout (cycle in progress)", body)
        else:
            body = (f"Satellite: {sat_name} ({sat_ip})\nTime: {now_str}\n\n"
                    "Ping not responding (100% packet loss).\n"
                    "Monitoring for backhaul disconnection — power cycle will trigger\n"
                    "automatically if the satellite loses backhaul connection.")
            alert_email(f"\U0001f527 Orbi: {sat_name} ping timeout", body, is_error=True)

        _set_ping_state(sat_name, "PING_WAITING_MANUAL")
        return

    # PING_WAITING_MANUAL: no further action until ping recovers


def handle_ping_recovery(sat_name, sat_ip):
    """Called when ping recovers. Resets state and sends recovery alert."""
    prev = _ping_state(sat_name)
    if prev is None:
        return
    _set_ping_state(sat_name, None)
    now_str = datetime.now().strftime("%d %b %Y %H:%M")
    log.info(f"PING {sat_name}: recovered (was {prev})")
    db_insert_ping_event(sat_name, "RECOVERED", detail=prev)
    body = (f"Satellite: {sat_name} ({sat_ip})\nTime: {now_str}\n\n"
            f"Ping responding again.\nPrevious state: {prev}")
    alert_email(f"\u2705 Orbi: {sat_name} ping recovered", body)

# ─── Ping monitoring ───────────────────────────────────────────────────────────
_known_satellites = {}
_known_satellites_lock = threading.Lock()
_test_cycle_active = set()  # satellite names currently in manual test cycle

def ping_burst(ip, count=10, packet_size=100, timeout=2):
    """
    Send `count` pings of `packet_size` bytes to `ip`.
    Returns dict:
      success      : bool  (at least one reply received)
      packet_loss  : float (0.0 – 100.0 percent)
      latency_ms   : float (last recorded latency, for legacy compat)
      latency_min  : float
      latency_max  : float
      latency_avg  : float
    Uses a single ping -c <count> call for efficiency.
    """
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout),
             "-s", str(packet_size), ip],
            capture_output=True, text=True, timeout=count * timeout + 5
        )
        output = result.stdout

        # Parse packet loss: "10 packets transmitted, 8 received, 20% packet loss"
        loss_pct = 100.0
        for line in output.splitlines():
            if "packet loss" in line:
                for part in line.split():
                    if part.endswith("%"):
                        try:
                            loss_pct = float(part.rstrip("%"))
                        except ValueError:
                            pass
                break

        # Parse rtt stats: "rtt min/avg/max/mdev = 1.234/2.345/3.456/0.123 ms"
        lat_min = lat_avg = lat_max = None
        for line in output.splitlines():
            if "rtt" in line and "min/avg/max" in line:
                try:
                    stats_part = line.split("=")[1].strip().split("/")
                    lat_min = float(stats_part[0])
                    lat_avg = float(stats_part[1])
                    lat_max = float(stats_part[2])
                except (IndexError, ValueError):
                    pass
                break

        success = loss_pct < 100.0
        return {
            "success":     success,
            "packet_loss": loss_pct,
            "latency_ms":  lat_avg,   # use avg as primary latency
            "latency_min": lat_min,
            "latency_max": lat_max,
            "latency_avg": lat_avg,
        }
    except Exception as e:
        return {
            "success":     False,
            "packet_loss": 100.0,
            "latency_ms":  None,
            "latency_min": None,
            "latency_max": None,
            "latency_avg": None,
        }

def ping_worker():
    while True:
        # ── Satellite ping burst ───────────────────────────────────────────────
        with _known_satellites_lock:
            satellites = dict(_known_satellites)
        for name, ip in satellites.items():
            r = ping_burst(ip)
            db_insert_ping(
                name, ip,
                latency_ms  = r["latency_ms"],
                success     = r["success"],
                latency_min = r["latency_min"],
                latency_max = r["latency_max"],
                latency_avg = r["latency_avg"],
                packet_loss = r["packet_loss"],
            )
            # Drive ping timeout state machine
            if r["packet_loss"] == 100.0:
                if name in _test_cycle_active:
                    log.info(f"PING {name}: timeout suppressed (test cycle in progress)")
                else:
                    handle_ping_timeout(name, ip)
            else:
                handle_ping_recovery(name, ip)

        # ── Plug ICMP ping — update last_seen on success ───────────────────────
        with _tapo_lock:
            plugs = {k: v for k, v in _tapo_config.get("plugs", {}).items()
                     if v.get("enabled") and
                     v.get("ip", "CONFIGURE_ME") not in ("CONFIGURE_ME", "", None)}
        for sat_name, plug_cfg in plugs.items():
            ip        = plug_cfg["ip"]
            reachable = probe_plug(ip)
            # Update last_seen and reachable status immediately on success
            if reachable:
                with _tapo_lock:
                    if sat_name in _tapo_config.get("plugs", {}):
                        _tapo_config["plugs"][sat_name]["last_seen"] = \
                            datetime.now().strftime("%Y-%m-%d %H:%M")
                        save_tapo_config()
                if sat_name in _tapo_plug_status:
                    _tapo_plug_status[sat_name]["reachable"] = True
                # If plug just came back and we need to send the on() command
                if _disconnect_state.get(sat_name) == "POWER_ON_PENDING":
                    log.info(f"PLUG {sat_name}: reconnected — sending turn on")
                    now_str = datetime.now().strftime("%d %b %Y %H:%M")
                    try:
                        success = _run_async(_tapo_turn_on_with_retry(sat_name, retries=3, delay=10))
                        if success:
                            alert_email(
                                f"⚡ Orbi: {sat_name} plug turned on after reconnect",
                                f"Satellite: {sat_name}\nTime: {now_str}\n\n"
                                f"Plug reconnected to network and has been turned on.\n"
                                f"Waiting for satellite to recover..."
                            )
                            _disconnect_state[sat_name] = "POWER_CYCLED"
                        else:
                            log.warning(f"PLUG {sat_name}: turn on after reconnect failed")
                    except Exception as e:
                        log.warning(f"PLUG {sat_name}: turn on after reconnect error: {e}")

                # If satellite is still disconnected and we were waiting for
                # the plug to come back — now trigger the power cycle
                if _disconnect_state.get(sat_name) == "WAITING_PLUG":
                    log.info(f"PLUG {sat_name}: back online — triggering power cycle")
                    sat_info = {"name": sat_name, "ip": ip}
                    attempted, success, err = power_cycle_plug(sat_name)
                    now_str = datetime.now().strftime("%d %b %Y %H:%M")
                    if success:
                        alert_email(
                            f"⚡ Orbi: {sat_name} power cycled (plug back online)",
                            f"Satellite: {sat_name}\nTime: {now_str}\n\n"
                            f"Plug came back online — power cycle triggered.\n"
                            f"Waiting for satellite to recover..."
                        )
                        _disconnect_state[sat_name] = "POWER_CYCLED"
                    else:
                        alert_email(
                            f"🚨 ORBI ALERT: {sat_name} — power cycle failed after plug recovery",
                            f"Satellite: {sat_name}\nTime: {now_str}\n\n"
                            f"Plug came back but power cycle failed.\nError: {err}\n"
                            f"Manual intervention required.",
                            high_priority=True
                        )
                        _disconnect_state[sat_name] = "WAITING_MANUAL"
            else:
                if sat_name in _tapo_plug_status:
                    _tapo_plug_status[sat_name]["reachable"] = False

        time.sleep(PING_INTERVAL)

# ─── Daily summary scheduler ───────────────────────────────────────────────────
def scheduler_worker():
    """Fire daily summary at midnight."""
    while True:
        now  = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_secs = (next_midnight - now).total_seconds()
        time.sleep(sleep_secs)
        # Retry loop — exponential backoff up to 30 minutes
        delay = 60
        for attempt in range(6):
            try:
                send_daily_summary()
                db_cleanup()
                break  # Success
            except Exception as e:
                log.error(f"Daily summary failed (attempt {attempt+1}): {e}")
                if attempt < 5:
                    log.info(f"Retrying daily summary in {delay}s...")
                    time.sleep(delay)
                    delay = min(delay * 2, 1800)  # cap at 30 mins
                else:
                    log.error("Daily summary failed after 6 attempts — giving up")
                    alert_email(
                        "🔧 Orbi Monitor: Daily Summary Failed",
                        f"Could not send daily summary after 6 attempts.\n"
                        f"Last error: {e}",
                        is_error=True
                    )

# ─── Playwright scraper ────────────────────────────────────────────────────────
def get_satellite_status():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ).new_page()
        try:
            page.goto(f"http://{ORBI_IP}", timeout=30000, wait_until="networkidle")
            page.wait_for_selector("iframe#contentframe", timeout=10000)
            frame = page.frame(name="contentframe") or page.frames[1]

            frame.wait_for_selector("input[type='password']", timeout=20000)
            page.wait_for_timeout(3000)
            uf = frame.query_selector("input[type='text']")
            if uf:
                uf.click(); uf.fill(ORBI_USER); page.wait_for_timeout(300)
            pf = frame.query_selector("input[type='password']")
            pf.click(); pf.fill(ORBI_PASSWORD); page.wait_for_timeout(300)
            pf.press("Enter")

            # Wait for auth token
            for i in range(30):
                if "auth_token" in {c["name"] for c in page.context.cookies()}:
                    break
                page.wait_for_timeout(500)
            else:
                alert_email(
                    "🔧 Orbi Monitor: Login Failed",
                    f"Could not log in to Orbi at {ORBI_IP}.\n"
                    f"Time: {datetime.now().strftime('%d %b %Y %H:%M')}\n\n"
                    "Check password and router accessibility.",
                    is_error=True
                )
                return None

            # Wait for home page
            home_frame = None
            for f in page.frames:
                try:
                    f.wait_for_selector("text='Number of Satellites:'", timeout=30000)
                    home_frame = f; break
                except Exception:
                    continue
            if not home_frame:
                alert_email("🔧 Orbi Monitor: Page Load Failed",
                            f"Logged in but home page did not load.\nTime: {datetime.now().strftime('%d %b %Y %H:%M')}",
                            is_error=True)
                return None

            # Navigate to Attached Devices
            home_frame.locator("text='Attached Devices'").first.click()
            sat_frame = None
            for f in page.frames:
                try:
                    f.wait_for_selector("text='Connected Satellites'", timeout=20000)
                    sat_frame = f; break
                except Exception:
                    continue
            if not sat_frame:
                alert_email("🔧 Orbi Monitor: Satellite Table Not Found",
                            f"Could not find satellite table.\nTime: {datetime.now().strftime('%d %b %Y %H:%M')}",
                            is_error=True)
                return None

            page.wait_for_timeout(2000)
            return scrape_satellite_table(sat_frame)

        except Exception as e:
            log.error(f"Browser error: {e}")
            alert_email("🔧 Orbi Monitor: Error",
                        f"Unexpected error: {e}\nTime: {datetime.now().strftime('%d %b %Y %H:%M')}",
                        is_error=True)
            return None
        finally:
            try:
                logged_out = False
                btn = page.query_selector("button#logout")
                if btn and btn.is_visible():
                    btn.click(); page.wait_for_timeout(1500)
                    logged_out = True
                else:
                    for f in page.frames:
                        try:
                            b = f.query_selector("button#logout")
                            if b and b.is_visible():
                                b.click(); page.wait_for_timeout(1500)
                                logged_out = True; break
                        except Exception:
                            continue
                if not logged_out:
                    log.warning("Logout button not found")
                    ts = datetime.now().strftime('%d %b %Y %H:%M')
                    msg = (f"Could not find the logout button after check.\n"
                           f"Time: {ts}\n\n"
                           f"The router session may remain open. "
                           f"If this persists, subsequent logins may be blocked.")
                    alert_email("🔧 Orbi Monitor: Logout Failed", msg, is_error=True)
            except Exception as e:
                log.warning(f"Logout error: {e}")
            browser.close()

def scrape_satellite_table(frame):
    satellites = []
    try:
        sat_container = (frame.query_selector("#satellite_table") or
                         frame.query_selector(".el-table"))
        if not sat_container:
            return []
        body = sat_container.query_selector(".el-table__body-wrapper .el-table__body tbody")
        if not body:
            return []
        for row in body.query_selector_all("tr"):
            cells = row.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [c.inner_text().strip() for c in cells]
            ip = texts[2] if len(texts) > 2 else ""
            name_lines = [l.strip() for l in texts[1].split("\n") if l.strip()]
            name = next((l for l in name_lines if "RBS" not in l and "." not in l), "Unknown")
            conn_type = texts[4] if len(texts) > 4 else "Unknown"
            co_lines = [l.strip() for l in texts[5].split("\n") if l.strip()] if len(texts) > 5 else []
            connected_orbi = co_lines[0].replace("\xa0", " ") if co_lines else ""
            raw = texts[6].strip().lower() if len(texts) > 6 else ""
            backhaul = ("Good" if "good" in raw else
                        "Poor" if "poor" in raw else
                        "Disconnected" if "disconnect" in raw or raw == "" else
                        texts[6].strip())
            satellites.append({
                "name": name, "ip": ip, "mac": texts[3] if len(texts) > 3 else "",
                "connection_type": conn_type, "connected_orbi": connected_orbi,
                "backhaul_status": backhaul,
            })
    except Exception as e:
        log.error(f"Scrape error: {e}")
    return satellites

# ─── Change detection ──────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def check_for_changes(satellite, state):
    changes = []
    name = satellite["name"]
    prev = state.get(name, {})
    if not prev: return changes
    curr_s = satellite["backhaul_status"]
    prev_s = prev.get("backhaul_status", "Unknown")
    curr_t = satellite["connection_type"]
    prev_t = prev.get("connection_type", "Unknown")
    curr_score = backhaul_score(curr_t, curr_s)
    prev_score = backhaul_score(prev_t, prev_s)
    if curr_s == "Disconnected" and prev_s != "Disconnected":
        changes.append({"type": "disconnected"})
    elif curr_s != "Disconnected" and prev_s == "Disconnected":
        changes.append({"type": "reconnected", "score": curr_score})
    elif curr_score != prev_score:
        changes.append({"type": "score_change",
                        "from_label": f"{prev_t} {prev_s}",
                        "to_label":   f"{curr_t} {curr_s}",
                        "from_score": prev_score, "to_score": curr_score})
    return changes

# ─── Web dashboard ─────────────────────────────────────────────────────────────
DASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Orbi Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;color:#333}
header{background:#1a237e;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:10px}
header h1{font-size:1.3rem}
header span{margin-left:auto;font-size:0.8rem;opacity:0.75}
.wrap{max-width:1100px;margin:20px auto;padding:0 16px}
.section{font-size:0.8rem;font-weight:bold;color:#888;text-transform:uppercase;letter-spacing:0.06em;margin:20px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-bottom:8px}
.card{background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);border-left:5px solid #ccc}
.card.good{border-left-color:#2e7d32}.card.poor{border-left-color:#e65100}.card.disconnected{border-left-color:#b71c1c}
.card-name{font-size:1rem;font-weight:bold;margin-bottom:8px}
.card-row{display:flex;justify-content:space-between;font-size:0.82rem;padding:3px 0;border-bottom:1px solid #f5f5f5}
.card-row:last-child{border:none}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.75rem;font-weight:bold;color:#fff}
.badge.good{background:#2e7d32}.badge.poor{background:#e65100}.badge.disconnected{background:#b71c1c}.badge.unknown{background:#888}
.chart-wrap{background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);margin-bottom:14px;overflow:hidden}
.chart-title{font-size:0.9rem;font-weight:bold;color:#444;margin-bottom:8px}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 16px;font-size:0.78rem}
.leg-item{display:flex;align-items:center;gap:4px}
.leg-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.plug-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-bottom:16px}
.plug-card{background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.08);border-left:5px solid #ccc}
.plug-card.online{border-left-color:#2e7d32}.plug-card.offline{border-left-color:#b71c1c}
.plug-card.unconfigured{border-left-color:#bbb}.plug-card.disabled{border-left-color:#bbb}
footer{text-align:right;font-size:0.75rem;color:#bbb;padding:8px 16px 20px}
</style>
</head>
<body>
<header>
<svg width="28" height="28" viewBox="0 0 32 32"><circle cx="16" cy="16" r="15" fill="white" fill-opacity="0.2"/>
<path d="M16 8L24 20H8Z" fill="white"/><circle cx="16" cy="22" r="2" fill="white"/></svg>
<h1>Orbi Monitor</h1><span style="margin-left:8px;font-size:0.8rem;opacity:0.7">v{{VERSION}}</span><span style="margin-left:auto;font-size:0.8rem;opacity:0.75">auto-refreshes every 60s</span>
</header>
<div class="wrap">
<div class="section">Current Status</div>
<div class="cards">{{CARDS}}</div>
<div class="legend">
<span class="leg-item"><span class="leg-dot" style="background:#2e7d32"></span>5G Good (1.0)</span>
<span class="leg-item"><span class="leg-dot" style="background:#f9a825"></span>5G Poor (0.75)</span>
<span class="leg-item"><span class="leg-dot" style="background:#1565c0"></span>2.4G Good (0.5)</span>
<span class="leg-item"><span class="leg-dot" style="background:#e65100"></span>2.4G Poor (0.25)</span>
<span class="leg-item"><span class="leg-dot" style="background:#b71c1c"></span>Disconnected (0)</span>
</div>
<div class="section">24h Charts</div>
{{CHARTS}}
{{TAPO}}
</div>
<footer>
  <span>Last check: {{LAST_CHECK}} &nbsp;·&nbsp; v{{VERSION}}</span>
  <button id="btn-summary" onclick="runSummary()" style="margin-left:16px;padding:3px 10px;background:#1a237e;color:white;border:none;border-radius:4px;cursor:pointer;font-size:0.75rem;">📊 Run Daily Summary</button>
</footer>
<script>
function runSummary() {
  var btn = document.getElementById("btn-summary");
  btn.disabled = true;
  btn.textContent = "⏳ Running...";
  fetch("/api/run_summary")
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.textContent = "✅ Summary sent";
      setTimeout(function() {
        btn.disabled = false;
        btn.textContent = "📊 Run Daily Summary";
      }, 10000);
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = "❌ Failed";
    });
}
function testCycle(satName) {
  if (!confirm('Power cycle ' + satName + '?\\n\\nPlug will turn OFF then back ON.\\nSatellite will disconnect briefly.\\nIf the plug connects via this satellite it may take a little longer to recover.')) return;
  var btn = document.getElementById('btn-' + satName);
  btn.disabled = true;
  btn.textContent = '⏳ Cycling...';
  fetch('/api/test_cycle/' + encodeURIComponent(satName))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.textContent = '✅ Cycle started';
      setTimeout(function() {
        btn.disabled = false;
        btn.textContent = '⚡ Test Power Cycle';
      }, 90000);
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = '❌ Failed';
      console.error(e);
    });
}
</script>
</body>
</html>"""

def build_dashboard():
    status_rows = db_get_latest_status()
    cards = ""
    if not status_rows:
        cards = '<p style="color:#bbb;padding:20px">No data yet...</p>'
    else:
        for name, ip, backhaul, conn_type, connected_orbi, ts, score in status_rows:
            css   = backhaul.lower() if backhaul else "unknown"
            badge = f'<span class="badge {css}">{backhaul}</span>'
            score_str = f"{score:.2f}" if score is not None else "—"
            ts_fmt = ts[:16].replace("T", " ") if ts else "—"
            ping_st = _ping_state(name)
            ping_badge = ""
            if ping_st:
                ping_label = ping_st.replace("PING_","").replace("_"," ").title()
                ping_col   = "#b71c1c" if "MANUAL" in ping_st or "UNRECOV" in ping_st else "#e65100"
                ping_badge = (f'<span class="badge" style="background:{ping_col};font-size:0.7rem;margin-left:6px">'
                              f'\u26a0 Ping: {ping_label}</span>')
            cards += f"""<div class="card {css}">
              <div class="card-name">{name}{ping_badge}</div>
              <div class="card-row"><span>IP</span><span>{ip}</span></div>
              <div class="card-row"><span>Backhaul</span>{badge}</div>
              <div class="card-row"><span>Connection</span><span>{conn_type}</span></div>
              <div class="card-row"><span>Score</span><span style="font-weight:bold">{score_str}</span></div>
              <div class="card-row"><span>Via</span><span>{connected_orbi}</span></div>
              <div class="card-row"><span>Checked</span><span>{ts_fmt}</span></div>
            </div>"""

    # Charts
    sat_names = [r[0] for r in status_rows] if status_rows else []
    charts = ""
    for name in sat_names:
        svg = build_svg_chart(name, hours=24, width=760, height=180)
        charts += f'<div class="chart-wrap"><div class="chart-title">{name}</div>{svg}</div>'
    if not charts:
        charts = '<p style="color:#bbb;padding:20px">No chart data yet...</p>'

    conn = sqlite3.connect(DB_FILE)
    last = conn.execute("SELECT MAX(timestamp) FROM satellite_status").fetchone()[0]
    conn.close()
    last_check = last[:16].replace("T", " ") if last else "Never"

    # Tapo plug section
    tapo_html = ""
    with _tapo_lock:
        plug_statuses = dict(_tapo_plug_status)
    plugs_cfg = _tapo_config.get("plugs", {})

    if plugs_cfg:
        plug_cards = ""
        for sat_name, plug_cfg in plugs_cfg.items():
            ip      = plug_cfg.get("ip", "CONFIGURE_ME")
            enabled = plug_cfg.get("enabled", False)
            status  = plug_statuses.get(sat_name, {})

            if ip == "CONFIGURE_ME":
                css, status_str = "unconfigured", "Not configured"
            elif not enabled:
                css, status_str = "disabled", "Disabled"
            elif status.get("reachable"):
                css, status_str = "online", "✅ Online"
            elif status.get("reachable") is False:
                # Check if waiting to power cycle when plug comes back
                if _disconnect_state.get(sat_name) == "WAITING_PLUG":
                    css, status_str = "offline", "⚠️ Waiting to cycle"
                else:
                    css, status_str = "offline", "❌ Unreachable"
            else:
                css, status_str = "unconfigured", "⏳ Pending"

            _pw = status.get("power_w")
            _kw = status.get("today_kwh")
            _on = status.get("power_on")
            power_w   = f"{_pw:.1f}W"     if status.get("reachable") and _pw is not None else "—"
            today_kwh = f"{_kw:.3f} kWh"  if status.get("reachable") and _kw is not None else "—"
            power_on  = ("🟢 On" if _on else "🔴 Off") if (status.get("reachable") and _on is not None) else "—"
            nickname  = status.get("nickname", "")
            last_seen = plug_cfg.get("last_seen", "—")

            test_btn = ""
            if enabled and ip != "CONFIGURE_ME":
                test_btn = (f'<button id="btn-{sat_name}" onclick="testCycle(\'{sat_name}\')" '
                            f'style="margin-top:8px;width:100%;padding:6px;background:#1565c0;'
                            f'color:white;border:none;border-radius:4px;cursor:pointer;'
                            f'font-size:0.8rem;">⚡ Test Power Cycle</button>')
            plug_cards += f"""<div class="plug-card {css}">
              <div class="card-name">{sat_name}{f" ({nickname})" if nickname else ""}</div>
              <div class="card-row"><span>Status</span><span>{status_str}</span></div>
              <div class="card-row"><span>IP</span><span>{ip}</span></div>
              <div class="card-row"><span>Power</span><span>{power_on}</span></div>
              <div class="card-row"><span>Current</span><span>{power_w}</span></div>
              <div class="card-row"><span>Today</span><span>{today_kwh}</span></div>
              <div class="card-row"><span>Last seen</span><span>{last_seen}</span></div>
              {test_btn}
            </div>"""

        tapo_html = f"""
            <div class="section">Smart Plugs</div>
            <div class="plug-cards">{plug_cards}</div>
            <p style="font-size:0.75rem;color:#aaa;margin-bottom:16px;">
              Edit <code>/data/tapo_config.json</code> to configure plugs. Set <code>enabled: true</code> and enter the plug IP to activate.
            </p>"""
    else:
        tapo_html = """<div class="section">Smart Plugs</div>
            <p style="color:#bbb;padding:10px 0 16px;font-size:0.85rem;">
              No plugs configured yet. Edit <code>/data/tapo_config.json</code> to add plugs.
            </p>"""

    dash_version = os.environ.get("APP_VERSION", "unknown")
    return (DASH_HTML
            .replace("{{CARDS}}", cards)
            .replace("{{CHARTS}}", charts)
            .replace("{{TAPO}}", tapo_html)
            .replace("{{LAST_CHECK}}", last_check)
            .replace("{{VERSION}}", dash_version))

class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                try:
                    html = build_dashboard()
                except Exception as dash_err:
                    log.error(f"Dashboard build error: {dash_err}")
                    html = f"<html><body><h2>Dashboard Error</h2><pre>{dash_err}</pre></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            elif self.path == "/api/status":
                rows = db_get_latest_status()
                data = [{"name":r[0],"ip":r[1],"backhaul":r[2],"conn_type":r[3],
                         "connected_orbi":r[4],"ts":r[5],"score":r[6]} for r in rows]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            elif self.path == "/api/run_summary":
                def _do_summary():
                    try:
                        send_daily_summary()
                        db_cleanup()
                    except Exception as e:
                        import traceback
                        log.error(f"Manual summary failed: {e}\n{traceback.format_exc()}")
                threading.Thread(target=_do_summary, daemon=True).start()
                resp = json.dumps({"status": "ok", "message": "Daily summary triggered"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp.encode())
            elif self.path.startswith("/api/test_cycle/"):
                sat_name = self.path.split("/api/test_cycle/")[1]
                if is_plug_configured(sat_name):
                    def _do_test_cycle(name):
                        async def _cycle():
                            ip = _tapo_config["plugs"][name]["ip"]
                            plug_type, device = await _get_plug_client(name)
                            log.info(f"TEST CYCLE {name}: turning off ({plug_type})")
                            alert_email(
                                f"⚡ Orbi: {name} manual test cycle started",
                                f"A manual power cycle test was triggered via the dashboard.\n"
                                f"Plug: {name}\nIP: {ip}\n"
                                f"Plug will be OFF for 30s then turned back ON."
                            )
                            if plug_type == "kasa":
                                await device.turn_off()
                            else:
                                await device.off()
                            await asyncio.sleep(30)
                            # Retry turn-on — plug may have lost wifi during off period
                            log.info(f"TEST CYCLE {name}: turning on (with retry)")
                            success = await _tapo_turn_on_with_retry(name, retries=5, delay=10)
                            if success:
                                log.info(f"TEST CYCLE {name}: complete")
                            else:
                                log.warning(f"TEST CYCLE {name}: turn on failed after retries — plug may need manual intervention")
                                alert_email(
                                    f"🔧 Orbi: {name} test cycle — could not turn plug back on",
                                    f"Test cycle for {name} could not turn the plug back on after 5 attempts.\n"
                                    f"Please check the plug manually.",
                                    is_error=True
                                )
                        asyncio.run(_cycle())
                    _test_cycle_active.discard(sat_name)
                    _test_cycle_active.add(sat_name)
                    t = threading.Thread(target=_do_test_cycle, args=(sat_name,), daemon=True)
                    t.start()
                    resp = json.dumps({"status": "ok", "message": f"Test cycle started for {sat_name} — off for 30s then back on"})
                else:
                    resp = json.dumps({"status": "error", "message": f"{sat_name} not configured"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.encode())
            else:
                self.send_response(404); self.end_headers()
        except BrokenPipeError:
            pass  # Client disconnected before response completed — harmless
        except Exception as e:
            log.debug(f"Dashboard request error: {e}")
    def log_message(self, *args): pass

def dashboard_worker():
    HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashHandler).serve_forever()

# ─── Main loop ─────────────────────────────────────────────────────────────────
def run():
    VERSION = os.environ.get("APP_VERSION", "unknown")

    log.info(f"Orbi Monitor {VERSION} starting | {ORBI_IP} | check:{CHECK_INTERVAL}s ping:{PING_INTERVAL}s")
    init_db()
    load_tapo_config()
    check_tapo_login()

    threading.Thread(target=ping_worker,      daemon=True).start()
    threading.Thread(target=dashboard_worker, daemon=True).start()
    threading.Thread(target=scheduler_worker, daemon=True).start()

    send_email(
        f"✅ Orbi Monitor {VERSION} started",
        f"Monitoring: {ORBI_IP}\nBackhaul check: every {CHECK_INTERVAL//60} min\n"
        f"Ping: every {PING_INTERVAL}s\nDashboard: http://YOUR-UNRAID-IP:{DASHBOARD_PORT}\n"
        f"Daily summary: midnight",
        html_body=f"""<html><body><div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;">
        <div style="background:#1a237e;color:white;padding:12px 20px;border-radius:6px 6px 0 0;">
          <h2 style="margin:0;">✅ Orbi Monitor {VERSION} started</h2></div>
        <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 6px 6px;">
          <p>Monitoring <b>{ORBI_IP}</b></p>
          <ul style="margin:10px 0 0 16px;line-height:1.8">
            <li>Backhaul check every {CHECK_INTERVAL//60} minutes</li>
            <li>Ping every {PING_INTERVAL} seconds</li>
            <li>Daily summary at midnight</li>
            <li>Alerts for: disconnect, login failure, errors</li>
          </ul>
        </div></div></body></html>"""
    )

    state = load_state()

    while True:
        try:
            satellites = get_satellite_status()

            if satellites is None:
                pass  # alert already sent by get_satellite_status
            elif not satellites:
                alert_email("🔧 Orbi Monitor: No Satellites Found",
                            f"Satellite table loaded but no data parsed.\n"
                            f"Time: {datetime.now().strftime('%d %b %Y %H:%M')}",
                            is_error=True)
            else:
                db_insert_status(satellites)
                with _known_satellites_lock:
                    for s in satellites:
                        _known_satellites[s["name"]] = s["ip"]

                # Update Tapo plug statuses and auto-discover new satellites
                update_plug_statuses(satellites)

                for sat in satellites:
                    name    = sat["name"]
                    changes = check_for_changes(sat, state)
                    for c in changes:
                        if c["type"] == "disconnected":
                            handle_disconnect_state(name, sat, state)
                        elif c["type"] == "reconnected":
                            handle_reconnect(name, sat, c["score"])
                        elif c["type"] == "score_change":
                            log.info(f"STATUS {name}: {c['from_label']} ({c['from_score']:.2f}) → {c['to_label']} ({c['to_score']:.2f})")

                    # Drive disconnect state machine if already disconnected
                    if sat["backhaul_status"] == "Disconnected" and not changes:
                        handle_disconnect_state(name, sat, state)

                    state[name] = {
                        "backhaul_status": sat["backhaul_status"],
                        "connection_type": sat["connection_type"],
                        "connected_orbi":  sat["connected_orbi"],
                        "ip":              sat["ip"],
                        "last_checked":    datetime.now().isoformat()
                    }
                save_state(state)

        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()

# END OF FILE
