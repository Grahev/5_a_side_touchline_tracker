# Touchline Tracker

A phone-friendly match tracker for kids' 5-a-side, self-hosted on your homelab. It stores data in PostgreSQL, takes automatic nightly backups, and uses a login with no public sign-up.

- **No sign-up page.** Only you create accounts, from the server.
- **Works with no signal.** The phone keeps a copy and uploads when it's back online. The top bar shows *Saved to server*, *Saving…* or *Offline · saved on phone*.
- **Private database.** PostgreSQL runs on an internal Docker network with no ports open, so nothing outside the stack can reach it.
- **Backups.** A compressed backup runs every night, keeping 7 daily, 4 weekly and 6 monthly copies in a folder you choose.

## What's in it

- **Match tab:** tap to log goals, assists, saves, subs and the keeper. Every player's minutes are timed automatically.
- **Fair-play alerts:** bench cards turn yellow after the bench alert time, and the phone buzzes. A banner lists kids who are behind on minutes today across all games.
- **Plan tab:** mark who's here, set match length and sub and keeper intervals, then **Make plan**. The rotation shares minutes and keeper turns, and counts earlier games today. During the match a bar shows the next change with an **Apply** button. Players marked without 🧤 in Squad are left out of keeper turns.
- **Season tab:** totals, charts (team goals per game, or one child's minutes, goals and time in goal), and results by day. Tap a game for its goal timeline.
- **Teams tab:** record against each opponent.
- **Parent link (Squad tab):** a read-only page with the live score, goal timeline and today's minutes. Anyone with the link can view it, nobody can edit it, and you can replace or turn off the link at any time.
- **Two phones live:** sign in with the same account on two phones. Both can log at once, for example one doing subs and one doing goals, and changes merge within a few seconds.

```
├── app.py              server, login, database (PostgreSQL or SQLite)
├── static/index.html   the tracker
├── static/core.js      shared match logic (replays events into minutes, score, keeper)
├── static/share.html   read-only parent page (/s/<link>)
├── static/login.html   sign-in page
├── requirements.txt    PostgreSQL driver
├── Dockerfile
└── docker-compose.yml  app + PostgreSQL + backups
```

## 1. Deploy with Portainer

The stack has three containers: **touchline** (the app), **touchline-db** (PostgreSQL 16) and **touchline-backup** (nightly backups).

**a) Make a backup folder on the server** (once, over SSH):

```bash
sudo mkdir -p /srv/backups/touchline
sudo chown -R 999:999 /srv/backups/touchline   # the backup container writes as user 999
```

Put it on a different disk, or on your NAS, if you can. A backup on the same disk as the database won't survive that disk failing.

**b) Create the stack:**

1. In Portainer, go to **Stacks → Add stack** and name it `touchline`.
2. Choose **Build method: Repository**.
   - Repository URL: `https://github.com/Grahev/5_a_side_touchline_tracker`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
3. Under **Environment variables**, add:

   | Name | Value |
   |---|---|
   | `POSTGRES_PASSWORD` | a long random password, e.g. from `openssl rand -base64 32` |
   | `BACKUP_DIR` | `/srv/backups/touchline` |
   | `APP_PORT` | `8080`, or another free port (optional) |
   | `COOKIE_SECURE` | `1` once it's behind HTTPS (optional) |

4. Optionally, turn on **GitOps updates** with polling, for example every 5 minutes. Portainer then redeploys automatically when you push to GitHub.
5. Click **Deploy the stack**. The first build takes a minute or two.

The password only lives in Portainer. It is never in the code or the repo.

**c) Create your account:**

1. Go to **Containers → touchline → Console** and click **Connect** (use `/bin/sh`).
2. Run:

```sh
python app.py adduser dad
```

Then open `http://<homelab-ip>:8080` on your phone and sign in.

### Without Portainer

Put the same variables in a `.env` file next to `docker-compose.yml`, then run:

```bash
docker compose up -d --build
docker compose exec app python app.py adduser dad
```

## 2. Managing accounts

Run these in the **touchline** container's console:

| Task | Command |
|---|---|
| Add account | `python app.py adduser NAME` |
| Change password (also signs them out everywhere) | `python app.py passwd NAME` |
| Delete account and its data | `python app.py deluser NAME` |
| List accounts | `python app.py users` |

Each account has its own squad, matches and opponents. To track the same team from two phones, sign in with the same account on both.

After 10 wrong passwords from one address, login is blocked for 15 minutes. A sign-in lasts 90 days, set by `SESSION_DAYS`.

## 3. Backups and restore

**Automatic.** Backups land in `BACKUP_DIR`:

- `last/` holds every backup with date and time, e.g. `touchline-20261018-000000.sql.gz`.
- `daily/`, `weekly/` and `monthly/` hold the kept copies.

One is also taken whenever the stack starts. Old ones are removed automatically.

**Take one now:**

```bash
docker exec touchline-backup /backup.sh
```

**Off-site copy (recommended).** Sync `BACKUP_DIR` somewhere else, such as a NAS, a USB disk, or cloud storage with `rclone`.

**Restore a backup.** This replaces everything in the database with the backup.

```bash
docker stop touchline                                   # stop the app so nothing writes meanwhile
docker exec touchline-db psql -U touchline -d touchline -c "DROP SCHEMA public CASCADE"
zcat /srv/backups/touchline/last/touchline-YYYYMMDD-HHMMSS.sql.gz | docker exec -i touchline-db psql -q -U touchline -d touchline
docker start touchline
```

**From your phone.** The Squad tab has **Download backup (JSON)** for your own account's data.

## 4. Moving from the earlier SQLite version

If you already ran the SQLite version, its data is in a file called `touchline.db`. To copy it into PostgreSQL:

```bash
docker cp /path/to/touchline.db touchline:/tmp/old.db
docker exec touchline python app.py import-sqlite /tmp/old.db
```

Accounts, sessions, squads, matches and parent links all come across. It's safe to run more than once, because rows that are already there are skipped.

## 5. Using it at the pitch (away from home Wi-Fi)

Your phone needs to reach the homelab over mobile data. Pick one:

- **Tailscale (easiest and private).** Install it on the homelab and your phone, then open `http://<homelab-tailscale-name>:8080`. Nothing is exposed to the internet.
- **Cloudflare Tunnel.** Gives you a public `https://` address without opening router ports.
- **A reverse proxy you already run** (Caddy, Nginx Proxy Manager, Traefik). Point it at the app port.

When it's served over HTTPS, set `COOKIE_SECURE` to `1` in the stack's environment variables and redeploy.

**Tip:** open the site in your phone browser and choose **Add to Home Screen**. It then opens like an app.

## Security summary

- **Database:** PostgreSQL has no published port and sits on an internal-only network, so only the app and backup containers can reach it. It uses SCRAM password authentication.
- **App container:** runs as a non-root user with a read-only filesystem and `no-new-privileges`.
- **Passwords:** stored as salted PBKDF2-SHA256 hashes (600,000 rounds).
- **Sign-in sessions:** random tokens in an HttpOnly, SameSite cookie, with only a hash of each token stored. Changing a password signs that account out everywhere.
- **Parent links:** random and read-only, showing only names, scores and minutes. Making a new link stops the old one working.

## Data model

| Table | Holds |
|---|---|
| `state` | full app data per account (JSON text) |
| `matches` | one row per saved match: date, opponent, goals for/against, player stats as JSON |
| `users`, `sessions` | accounts and sign-ins |
| `shares` | the active parent link per account |

Example query of your record against each opponent:

```bash
docker exec -it touchline-db psql -U touchline -d touchline -c "
  SELECT opponent, COUNT(*) p,
         SUM((goals_for>goals_against)::int) w, SUM((goals_for=goals_against)::int) d,
         SUM((goals_for<goals_against)::int) l, SUM(goals_for) gf, SUM(goals_against) ga
  FROM matches GROUP BY opponent ORDER BY p DESC;"
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD` | required | Database password (used by app, database and backups) |
| `BACKUP_DIR` | required | Folder on the server for backups |
| `APP_PORT` | `8080` | Port opened on the server |
| `COOKIE_SECURE` | `0` | Set to `1` when using HTTPS |
| `SESSION_DAYS` | `90` | How long a sign-in lasts |
| `DATABASE_URL` | set by compose | PostgreSQL connection. If unset, the app falls back to a SQLite file at `DB_PATH`. |

## How syncing works

Every tap saves to the phone at once, then uploads to the server within a second. With no signal, it retries every 10 seconds and when the phone reconnects. When you open the app, it loads the latest data from the server, unless this phone has changes that haven't uploaded yet. In that case the phone's copy is uploaded first.

Match events are merged, not overwritten. If two phones save at the same moment, the server tells the second phone, which merges both sets of events and saves again. Undo hides the last tap's events on every phone. Squad and settings edits use the most recent change.
