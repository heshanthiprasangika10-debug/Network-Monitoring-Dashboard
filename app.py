"""
Network Monitoring Dashboard
Flask + SQLite + background monitor thread + email alerts
"""
import hmac
import os
import platform
import re
import secrets
import smtplib
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

# ---------------- CONFIG ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor.db")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))   # seconds between checks
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "2"))    # consecutive fails before DOWN
TIMEOUT = 2                                               # seconds per check

# Email alert settings (set as environment variables; if empty, alerts only print to console)
SMTP_HOST = os.getenv("SMTP_HOST", "")          # e.g. smtp.gmail.com
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")          # your gmail address
SMTP_PASS = os.getenv("SMTP_PASS", "")          # Gmail App Password
ALERT_TO = os.getenv("ALERT_TO", "")            # who receives alerts

# Login settings (CHANGE THESE! set as environment variables)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)   # random each run if not set

HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]+$")     # safe hostname/IP only (blocks command injection)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ---------------- DATABASE ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER,
                status TEXT DEFAULT 'unknown',
                fail_count INTEGER DEFAULT 0,
                last_checked TEXT
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                is_up INTEGER NOT NULL,
                latency REAL
            );
            CREATE INDEX IF NOT EXISTS idx_checks_dev_ts ON checks(device_id, ts);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER,
                device_name TEXT,
                ts TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        if c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO devices (name, host, port) VALUES (?,?,?)",
                [
                    ("Localhost", "127.0.0.1", None),
                    ("Google DNS", "8.8.8.8", None),
                    ("Google Web (HTTPS)", "google.com", 443),
                ],
            )


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- CHECKS ----------------
def ping(host):
    """ICMP ping. Returns (is_up, latency_ms)."""
    is_win = platform.system().lower() == "windows"
    cmd = ["ping", "-n", "1", "-w", str(TIMEOUT * 1000), host] if is_win \
        else ["ping", "-c", "1", "-W", str(TIMEOUT), host]
    start = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 2)
    except FileNotFoundError:
        print("[WARN] 'ping' command not found on this system - use a port (TCP) check instead")
        return False, None
    except Exception:
        return False, None
    if out.returncode != 0 or "unreachable" in out.stdout.lower():
        return False, None
    m = re.search(r"time[=<]\s*([\d.]+)", out.stdout)
    latency = float(m.group(1)) if m else round((time.time() - start) * 1000, 1)
    return True, latency


def tcp_check(host, port):
    """TCP port check. Returns (is_up, latency_ms)."""
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, round((time.time() - start) * 1000, 1)
    except Exception:
        return False, None


def check_device(dev):
    if dev["port"]:
        return tcp_check(dev["host"], dev["port"])
    return ping(dev["host"])


# ---------------- ALERTS ----------------
def send_email(subject, body):
    print(f"[ALERT] {subject}")
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and ALERT_TO):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_TO
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print("[EMAIL ERROR]", e)


def log_event(device_id, name, message):
    with db() as c:
        c.execute(
            "INSERT INTO events (device_id, device_name, ts, message) VALUES (?,?,?,?)",
            (device_id, name, now(), message),
        )


# ---------------- MONITOR LOOP ----------------
def run_cycle():
    with db() as c:
        devices = [dict(r) for r in c.execute("SELECT * FROM devices")]
    if not devices:
        return

    with ThreadPoolExecutor(max_workers=min(20, len(devices))) as pool:
        results = list(pool.map(check_device, devices))

    with db() as c:
        for dev, (is_up, latency) in zip(devices, results):
            ts = now()
            c.execute(
                "INSERT INTO checks (device_id, ts, is_up, latency) VALUES (?,?,?,?)",
                (dev["id"], ts, 1 if is_up else 0, latency),
            )
            old_status = dev["status"]
            fail_count = 0 if is_up else dev["fail_count"] + 1
            if is_up:
                new_status = "up"
            elif fail_count >= FAIL_THRESHOLD:
                new_status = "down"
            else:
                new_status = old_status if old_status != "unknown" else "unknown"

            c.execute(
                "UPDATE devices SET status=?, fail_count=?, last_checked=? WHERE id=?",
                (new_status, fail_count, ts, dev["id"]),
            )

            if new_status != old_status and new_status in ("up", "down") and old_status != "unknown":
                target = f"{dev['host']}:{dev['port']}" if dev["port"] else dev["host"]
                if new_status == "down":
                    msg = f"{dev['name']} ({target}) is DOWN"
                else:
                    msg = f"{dev['name']} ({target}) is back UP"
                c.execute(
                    "INSERT INTO events (device_id, device_name, ts, message) VALUES (?,?,?,?)",
                    (dev["id"], dev["name"], ts, msg),
                )
                threading.Thread(
                    target=send_email,
                    args=(f"[NetMon] {msg}", f"{msg}\nTime: {ts}"),
                    daemon=True,
                ).start()

        # keep DB small: delete checks older than 7 days
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))


def monitor_loop():
    while True:
        try:
            run_cycle()
        except Exception as e:
            print("[MONITOR ERROR]", e)
        time.sleep(CHECK_INTERVAL)


# ---------------- AUTH ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Login required"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        ok_u = hmac.compare_digest(u.encode(), ADMIN_USER.encode())
        ok_p = hmac.compare_digest(p.encode(), ADMIN_PASS.encode())
        if ok_u and ok_p:
            session.clear()
            session["user"] = u
            return redirect(url_for("index"))
        time.sleep(1)   # slow down brute-force attempts
        error = "Username or password is incorrect"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- API ----------------
@app.route("/")
@login_required
def index():
    return render_template("index.html", interval=CHECK_INTERVAL)


@app.route("/api/devices", methods=["GET"])
@login_required
def api_devices():
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    with db() as c:
        for d in c.execute("SELECT * FROM devices ORDER BY id"):
            stats = c.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(is_up),0) up FROM checks "
                "WHERE device_id=? AND ts>=?", (d["id"], since)
            ).fetchone()
            uptime = round(stats["up"] * 100 / stats["total"], 1) if stats["total"] else None
            hist = [
                r["latency"] for r in c.execute(
                    "SELECT latency FROM checks WHERE device_id=? ORDER BY id DESC LIMIT 30",
                    (d["id"],),
                )
            ][::-1]
            last = hist[-1] if hist else None
            out.append({
                "id": d["id"], "name": d["name"], "host": d["host"], "port": d["port"],
                "status": d["status"], "last_checked": d["last_checked"],
                "latency": last, "uptime": uptime, "history": hist,
            })
    return jsonify(out)


@app.route("/api/devices", methods=["POST"])
@login_required
def api_add_device():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    port = data.get("port")
    if not name or not host or not HOST_RE.match(host):
        return jsonify({"error": "Name and a valid host/IP are required"}), 400
    try:
        port = int(port) if port not in (None, "") else None
        if port is not None and not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        return jsonify({"error": "Port must be 1-65535"}), 400
    with db() as c:
        c.execute("INSERT INTO devices (name, host, port) VALUES (?,?,?)", (name, host, port))
    return jsonify({"ok": True}), 201


@app.route("/api/devices/<int:dev_id>", methods=["DELETE"])
@login_required
def api_delete_device(dev_id):
    with db() as c:
        c.execute("DELETE FROM checks WHERE device_id=?", (dev_id,))
        c.execute("DELETE FROM devices WHERE id=?", (dev_id,))
    return jsonify({"ok": True})


@app.route("/api/events")
@login_required
def api_events():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 20")]
    return jsonify(rows)


@app.route("/api/devices/<int:dev_id>/history")
@login_required
def api_history(dev_id):
    hours = request.args.get("hours", "1", type=int)
    hours = hours if hours in (1, 6, 24) else 1
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        dev = c.execute("SELECT name, host, port FROM devices WHERE id=?", (dev_id,)).fetchone()
        if not dev:
            return jsonify({"error": "Not found"}), 404
        rows = c.execute(
            "SELECT ts, is_up, latency FROM checks WHERE device_id=? AND ts>=? ORDER BY ts",
            (dev_id, since),
        ).fetchall()
    points = [{"ts": r["ts"], "up": r["is_up"], "latency": r["latency"]} for r in rows]
    step = max(1, len(points) // 300)          # downsample so chart stays fast
    points = points[::step]
    lats = [p["latency"] for p in points if p["latency"] is not None]
    summary = {
        "min": round(min(lats), 1) if lats else None,
        "avg": round(sum(lats) / len(lats), 1) if lats else None,
        "max": round(max(lats), 1) if lats else None,
        "failed": sum(1 for p in points if not p["up"]),
        "total": len(points),
    }
    return jsonify({"name": dev["name"], "points": points, "summary": summary})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()
    if ADMIN_PASS == "admin123":
        print("[WARN] Default password in use - set ADMIN_USER / ADMIN_PASS environment variables!")
    print(f"Dashboard: http://127.0.0.1:5000  (checks every {CHECK_INTERVAL}s)")
    app.run(host="0.0.0.0", port=5000, debug=False)
