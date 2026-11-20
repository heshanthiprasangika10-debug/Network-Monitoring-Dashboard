# NetMon – Network Monitoring Dashboard

Python (Flask) + SQLite + HTML/JS. Devices ping / port-check karala live dashboard ekak pennanawa, latency chart, login, saha device down unama email alert.

## Run karanna (Windows)

```bash
cd netmon
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set ADMIN_USER=heshi
set ADMIN_PASS=Hesha123
python app.py
```

Browser eke open karanna: http://127.0.0.1:5000  → login wenna.

> ADMIN_USER / ADMIN_PASS set nokaloth default `admin` / `admin123` use wenawa (console eke warning ekak enawa). Eka hamadama wenas karanna.
> Chart eka pennanna Chart.js CDN ekata internet oni.

## Chart eka
Dashboard eke device card ekak **click** karanna → latency chart eka open wenawa (1h / 6h / 24h). Fail una checks rathu ✕ widiyata pennanawa. `Esc` ekak ohoma close wenawa.

## Email alerts on karanna (optional)

Gmail App Password ekak hadala (Google Account > Security > 2-Step Verification > App passwords):

```bash
set SMTP_HOST=smtp.gmail.com
set SMTP_PORT=587
set SMTP_USER=heshanthiprasangika10@gmail.com
set SMTP_PASS=AppPw123
set ALERT_TO=heshanthiprasangika29@gmail.com
python app.py
```

(Linux/Mac wala `set` wenuwata `export` use karanna.)

## Settings

| Variable | Default | Meaning |
|---|---|---|
| ADMIN_USER / ADMIN_PASS | admin / admin123 | login details (WENAS KARANNA) |
| SECRET_KEY | random each run | session key (set kaloth restart unama login wenna wenawa) |
| CHECK_INTERVAL | 10 | seconds between checks |
| FAIL_THRESHOLD | 2 | consecutive fails ekak passe DOWN kiyala mark karanawa |

## Files

- `app.py` – backend + monitor thread + login + alerts + API
- `templates/index.html` – dashboard UI + latency chart
- `templates/login.html` – login page
- `monitor.db` – auto-create wenawa (SQLite)
