# Touchline Tracker – homelab install

A phone-friendly match tracker for kids' 5-a-side, with your data saved in a SQLite database on your own server.

- **No sign-up page.** Only you create accounts, from the server's command line.
- **Works with no signal.** The phone keeps a copy and uploads when it is back online. The top-right status shows *Saved to server*, *Saving…* or *Offline · saved on phone*.
- **Tiny.** Python standard library only. No packages to install.

## What's in it

- **Match tab:** tap to log goals, assists, saves, subs and the keeper. Every player's minutes are timed automatically.
- **Fair-play alerts:** bench cards turn yellow after the bench alert time, and the phone buzzes. A banner lists kids who are behind on minutes today across all games.
- **Plan tab:** mark who's here, set match length and sub and keeper intervals, then **Make plan**. The rotation shares minutes and keeper turns, and counts earlier games today. During the match a bar shows the next change with an **Apply** button. Players marked without 🧤 in Squad are left out of keeper turns.
- **Season tab:** totals, charts (team goals per game, or one child's minutes, goals and time in goal), and results by day. Tap a game for its goal timeline.
- **Teams tab:** record against each opponent.
- **Parent link (Squad tab):** a read-only page with the live score, goal timeline and today's minutes. Anyone with the link can view it, nobody can edit, and you can replace or turn off the link at any time.
- **Two phones live:** sign in with the same account on two phones. Both can log at the same time, for example one doing subs and one doing goals, and changes merge within a few seconds. The top bar shows "Live · 2 phones".

```
touchline/
├── app.py              server, login, database
├── static/index.html   the tracker
├── static/core.js      shared match logic (replays events into minutes, score, keeper)
├── static/share.html   read-only parent page (/s/<link>)
├── static/login.html   sign-in page
├── Dockerfile
└── docker-compose.yml
```

## 1. Run it

### Option A: Docker (recommended)

```bash
cd touchline
docker compose up -d --build
docker compose exec touchline python app.py adduser dad   # asks for a password (8+ chars)
```

Open `http://<homelab-ip>:8080` on your phone and sign in.

### Option B: Plain Python (3.9+)

```bash
cd touchline
python3 app.py adduser dad
python3 app.py            # serves on port 8080, database in ./data/touchline.db
```

To keep it running after reboots, use a systemd unit like this one (change the paths and user):

```ini
# /etc/systemd/system/touchline.service
[Unit]
Description=Touchline Tracker
After=network.target

[Service]
WorkingDirectory=/opt/touchline
ExecStart=/usr/bin/python3 /opt/touchline/app.py
Environment=DB_PATH=/opt/touchline/data/touchline.db
Restart=always
User=touchline

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now touchline
```

## 2. Managing accounts

| Task | Docker | Plain Python |
|---|---|---|
| Add account | `docker compose exec touchline python app.py adduser NAME` | `python3 app.py adduser NAME` |
| Change password (also signs them out everywhere) | `… python app.py passwd NAME` | `python3 app.py passwd NAME` |
| Delete account and its data | `… python app.py deluser NAME` | `python3 app.py deluser NAME` |
| List accounts | `… python app.py users` | `python3 app.py users` |

Each account has its own squad, matches and opponents. To track the same team from two phones, sign in with the same account on both.

After 10 wrong passwords from one address, login is blocked for 15 minutes. A sign-in lasts 90 days, set by `SESSION_DAYS`.

## 3. Using it at the pitch (away from home Wi-Fi)

Your phone needs to reach the homelab over mobile data. Pick one:

- **Tailscale (easiest and private).** Install Tailscale on the homelab and your phone, then open `http://<homelab-tailscale-name>:8080`. Nothing is exposed to the internet. `tailscale serve` can add HTTPS.
- **Cloudflare Tunnel.** Gives you a public `https://` address without opening router ports.
- **Reverse proxy you already run** (Caddy, Nginx Proxy Manager, Traefik). Point it at port 8080.

When it is served over HTTPS, set `COOKIE_SECURE: "1"` in `docker-compose.yml` and run `docker compose up -d`.

Example Caddy config:

```
touchline.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**Tip:** open the site in your phone browser and choose **Add to Home Screen**. It then opens like an app.

## 4. Your data

- **Database file:** `./data/touchline.db`. Back up this folder. For a safe live copy:
  `sqlite3 data/touchline.db ".backup data/backup-$(date +%F).db"`
- **Phone download:** the Squad tab has a **Download backup (JSON)** button.
- **Tables:**
  - `state`: the full app data per account
  - `matches`: one row per saved match (date, opponent, goals for and against, player stats as JSON), handy for your own queries
  - `users`, `sessions`: accounts and sign-ins (passwords are salted PBKDF2 hashes)
  - `shares`: the active parent link for each account

Example query of results against each opponent:

```bash
sqlite3 data/touchline.db "SELECT opponent, COUNT(*) p,
  SUM(goals_for>goals_against) w, SUM(goals_for=goals_against) d, SUM(goals_for<goals_against) l,
  SUM(goals_for) gf, SUM(goals_against) ga FROM matches GROUP BY opponent;"
```

## Upgrading from the first version

Replace the files and restart with `docker compose up -d --build`. Your database is kept, and any match in progress converts automatically.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port to listen on |
| `DB_PATH` | `data/touchline.db` | SQLite file location |
| `COOKIE_SECURE` | `0` | Set to `1` when using HTTPS |
| `SESSION_DAYS` | `90` | How long a sign-in lasts |
| `ADMIN_USER` / `ADMIN_PASSWORD` | unset | Creates this account on first start if it doesn't exist. Remove after first run. |

## How syncing works

Every tap saves to the phone at once, then uploads to the server within a second. With no signal, it retries every 15 seconds and when the phone reconnects. When you open the app, it loads the latest data from the server, unless this phone has changes that haven't uploaded yet. In that case the phone's copy is uploaded first.

Match events are merged, not overwritten. If two phones save at the same moment, the server tells the second phone, which merges both sets of events and saves again. Undo hides the last tap's events on every phone. Squad and settings edits use the most recent change.
