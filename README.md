# ✈️ Flight Price Tracker v3

Tracks flight prices across multiple routes using **fli** (Google Flights, no API key needed).
Sends notifications via **Email**, **WhatsApp** (CallMeBot), and **ntfy.sh**.
Runs daily for free on **GitHub Actions** and publishes a live **dashboard** via GitHub Pages.

---

## Setup in 5 steps

### 1 · Install dependencies
```bash
pip install flights click requests
```

### 2 · Configure
```bash
cp config.example.json config.json
```
Edit `config.json` — set your routes, notification channels, and price thresholds.

### 3 · Test locally
```bash
# Fast check (no live flight search)
python test.py --no-live

# Full check including a live search
python test.py

# Full check + send test notifications to all channels
python test.py --notify
```

### 4 · Create a private GitHub repo and push
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/flight-tracker.git
git push -u origin main
```
Use a **private** repo — your config will be stored as a secret, not in the repo itself.

### 5 · Add GitHub Secret
1. Go to your repo → **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `FLIGHT_CONFIG`
4. Value: paste the entire contents of your `config.json`
5. Save

The workflow runs automatically at **08:00 UTC (11:00 Romania time)** every day.
Trigger it manually anytime from the **Actions** tab → **Run workflow**.

---

## GitHub Pages dashboard

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main` · Folder: `/docs`
4. Save

Dashboard live at: `https://YOUR_USERNAME.github.io/flight-tracker/`

---

## Config reference

| Field | Description |
|---|---|
| `origin` / `destination` | IATA airport codes |
| `date_from` / `date_to` | Search window (YYYY-MM-DD) |
| `target_nights` | Ideal trip duration |
| `flexibility_days` | ±N nights checked per departure date |
| `daily_samples` | Departure dates checked per run (spread across window) |
| `passengers` | Number of adults |
| `currency` | EUR, USD, RON, etc. |
| `preferred_airline` | IATA code e.g. `TK` — leave empty for all |
| `max_stopovers` | Max number of stops |
| `max_layover_hours` | Max single layover duration |
| `bags` | Checked bags (0/1/2) |
| `departure_window.mode` | `hard` = filter out · `soft` = flag with ⭐ |
| `max_price_alert` | Alert when price drops below this |
| `notifications.daily.enabled` | Daily digest (only when below threshold if threshold is set) |
| `notifications.price_alert.enabled` | Instant alert when price ≤ threshold |
| `notifications.weekly_summary` | Weekly report on chosen day |
| `notifications.channels` | Per-route enable/disable per channel |

---

## Notification setup

### Email (Gmail)
1. Enable 2-factor auth on your Google account
2. Go to https://myaccount.google.com/apppasswords → create App Password
3. Use the 16-char app password as `password` in config

### WhatsApp (CallMeBot — free)
1. Save **+34 644 54 29 97** as a contact
2. Send `I allow callmebot to send me messages` to that number on WhatsApp
3. You'll receive your API key — put it in config

### ntfy.sh (push to phone — no account needed)
1. Install ntfy app (Android / iOS)
2. Subscribe to your topic name
3. Use the same topic in config

---

## Rate limiting

fli talks to Google Flights. Google may rate-limit rapid repeated calls from the same IP.
- **Locally**: if you hit a limit while testing, wait 20–30 min or use `--no-live` flag
- **GitHub Actions**: each daily run gets a fresh cloud IP — rate limiting is not an issue in production
