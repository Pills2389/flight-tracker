# Development Guide

## Setup

### 1. Prerequisites
- Python 3.10+
- Git
- VS Code with recommended extensions (see `.vscode/extensions.json`)

### 2. Install dependencies
```bash
pip install flights click requests
```

### 3. Configure
```bash
cp config.example.json config.json
# Edit config.json with your routes and credentials
```

### 4. Verify setup
```bash
python test.py --no-live
```

---

## Running locally

Use the VS Code **Run and Debug** panel (Ctrl+Shift+D) — all configurations are
pre-set in `.vscode/launch.json`:

| Config | Command |
|---|---|
| ▶ Run tracker (normal) | `python flight_tracker.py` |
| 🔍 Run tracker (debug mode) | `python flight_tracker.py --debug` |
| 🔍 Debug single route — OTP→AKL | `python flight_tracker.py --debug --route otp-akl-2027` |
| 🔍 Debug single route — OTP→HKG | `python flight_tracker.py --debug --route otp-hkd-2026` |
| ✅ Test suite (fast) | `python test.py --no-live` |
| ✅ Test suite (live) | `python test.py` |
| ✅ Test suite (notify) | `python test.py --notify` |

Or run from the terminal directly using the same commands.

---

## Debug mode

```bash
python flight_tracker.py --debug --route ROUTE_ID
```

Creates a `debug/` folder with one JSON file per API call, named:
```
debug/otp-akl-2027_2027-02-15_2027-03-07_143022.json
```

Console output shows:
```
[DEBUG] Saved 28 raw results → debug/otp-hkd-2026_...json
[DEBUG] Airlines in raw results: EK:12, LH:8, QR:4, TK:6
[DEBUG] Filtered out: 3 outbound too long, 2 return too long, 0 self-transfer → 17 kept
```

Use this to diagnose missing airlines or unexpected filtering.

---

## Making changes

### Adding a new filter
1. Add the config field to `config.example.json` with a `_note` comment
2. Read it in `search_route()` in `flight_tracker.py` where other filters are applied
3. Add to the `[DEBUG] Filtered out:` counter and log line
4. Update `CLAUDE.md` config schema section

### Adding a new notification channel
1. Add credentials block to the `notifications` section of `config.example.json`
2. Add a `send_CHANNEL()` function following the pattern of `send_email()` / `send_ntfy()`
3. Add it to `_active_channels()` and `dispatch()`
4. Add a test in `test.py`

### Adding a new route to your config
1. Copy an existing route block in `config.json`
2. Change `id`, `label`, `origin`, `destination`, dates, notification settings
3. Test locally: `python flight_tracker.py --debug --route YOUR_NEW_ROUTE_ID`
4. Update the `FLIGHT_CONFIG` GitHub Secret with the new config contents

### Changing the schedule
Open `.github/workflows/flight_check.yml` and edit the cron line:
```yaml
# Once daily at 10:00 Romania (07:00 UTC summer)
- cron: '0 7 * * *'

# Every 2 hours, 10:00–20:00 Romania summer
- cron: '0 7,9,11,13,15,17 * * *'
```

---

## Pushing changes to GitHub

GitHub Actions commits `price_history.json` and `docs/` back to the repo after
each run, so `origin/main` can drift ahead of your local branch between your
last pull and your push. **Never `git push --force`** — that overwrites the
bot's automated commits (and anyone else's work) with no way back. Instead:

```bash
git add .
git commit -m "Description of change"
git push
# if rejected as non-fast-forward:
git fetch origin
git log origin/main -3 --oneline   # confirm it's only the bot's "📊 ... prices updated" commit
git rebase origin/main             # replays your commit(s) on top — safe, no data loss
git push
```

This has come up twice already (bot commits landing mid-session); `fetch` +
`rebase origin/main` resolves it cleanly every time with no conflicts.

---

## GitHub Actions

The workflow (`.github/workflows/flight_check.yml`) runs `flight_tracker.py`, then
commits `price_history.json` and `docs/index.html` back to the repo.

**Config is stored as a secret** — never in the repo:
- Go to repo → Settings → Secrets and variables → Actions
- Secret name: `FLIGHT_CONFIG`
- Value: entire contents of your `config.json`

After any config change, update this secret.

---

## Dashboard (GitHub Pages)

Auto-generated at `docs/index.html` after each workflow run.
Live at: `https://pills2389.github.io/flight-tracker/`

Settings → Pages → Source: Deploy from branch `main`, folder `/docs`.

---

## Working with fli internals

A local clone of the **fli** library lives at `c:\Users\Cristi\Desktop\Repos\fli`.
When `pip install flights` behaviour is unclear (filter shapes, retry/backoff,
enum members, what `search()` actually returns for round trips, etc.), read the
source there directly instead of guessing — see [CLAUDE.md](CLAUDE.md) → "fli
source — read it locally instead of guessing" for the key files to start with.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Missing airline in results | Run `--debug`, check raw JSON. Add `"search_country": "RO"` |
| `fli` not found | `pip install flights click` |
| Config encoding error | Ensure `config.json` is saved as UTF-8 in VS Code |
| GitHub push rejected | Use `git push --force` |
| Workflow uses old config | Update `FLIGHT_CONFIG` secret on GitHub |
| ntfy emoji error | Fixed — Title header strips non-ASCII automatically |
| Rate limited locally | Wait 30 min or use `--no-live` flag for tests |
