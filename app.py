#!/usr/bin/env python3
"""Touchline Tracker server: static app + login + database storage.

Database: PostgreSQL when DATABASE_URL is set (recommended, see docker-compose.yml),
otherwise a local SQLite file (DB_PATH). SQLite needs nothing extra; PostgreSQL needs psycopg.

Usage:
  python app.py                       start the server (default)
  python app.py adduser NAME          create an account (asks for a password)
  python app.py passwd NAME           change a password
  python app.py deluser NAME          delete an account and its data
  python app.py users                 list accounts
  python app.py import-sqlite FILE    copy everything from an old SQLite database into PostgreSQL
"""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("DB_PATH", "data/touchline.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
PORT = int(os.environ.get("PORT", "8080"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"  # set to 1 when served over HTTPS
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "90"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 5 * 1024 * 1024
PBKDF2_ITER = 600_000
COOKIE = "tt_session"

_failed = {}  # ip -> [count, first_ts] simple login throttle
_seen = {}    # username -> {session hash: last seen}, used to show how many phones are live


def touch(user, token):
    now = time.time()
    d = _seen.setdefault(user, {})
    d[tok_hash(token or "")] = now
    for k in [k for k, t in d.items() if now - t > 20]:
        d.pop(k, None)
    return len(d)


# ---------- database ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS state(
  username TEXT PRIMARY KEY, data TEXT NOT NULL, version BIGINT NOT NULL, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS shares(
  token TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS matches(
  username TEXT NOT NULL, match_date TEXT NOT NULL, opponent TEXT, goals_for INTEGER,
  goals_against INTEGER, data TEXT NOT NULL, PRIMARY KEY(username, match_date))
"""
TABLES = ("users", "sessions", "state", "shares", "matches")


class PgConn:
    """Small wrapper so PostgreSQL is used exactly like sqlite3 in the rest of the code
    (same '?' placeholders, rows readable by column name, commit on leaving 'with')."""

    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.conn.close()

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)

    def executescript(self, script):
        for stmt in [x.strip() for x in script.split(";") if x.strip()]:
            self.conn.execute(stmt)


class SqliteConn:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, timeout=10)
        self.conn.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.conn.close()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executescript(self, script):
        self.conn.executescript(script)


def db():
    return PgConn() if USE_PG else SqliteConn()


def wait_for_db(seconds=60):
    """PostgreSQL may still be starting when the app container starts."""
    deadline = time.time() + seconds
    while True:
        try:
            with db() as c:
                c.execute("SELECT 1").fetchone()
            return
        except Exception as e:
            if time.time() > deadline:
                raise SystemExit(f"Database not reachable: {e}")
            print("Waiting for database…", e.__class__.__name__)
            time.sleep(2)


def init_db():
    if USE_PG:
        wait_for_db()
    else:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with db() as c:
            c.execute("PRAGMA journal_mode=WAL")
    with db() as c:
        c.executescript(SCHEMA)
    # optional first-run admin from environment
    u, p = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
    if u and p:
        with db() as c:
            if not c.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
                c.execute("INSERT INTO users VALUES(?,?,?)", (u, hash_pw(p), int(time.time())))
                print(f"Created account '{u}' from ADMIN_USER")


# ---------- passwords & sessions ----------
def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF2_ITER).hex()
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${h}"


def check_pw(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


def new_session(username):
    t = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)",
                  (tok_hash(t), username, int(time.time()) + SESSION_DAYS * 86400))
    return t


def session_user(token):
    if not token:
        return None
    with db() as c:
        r = c.execute("SELECT username FROM sessions WHERE token_hash=? AND expires>?",
                      (tok_hash(token), int(time.time()))).fetchone()
        if r and c.execute("SELECT 1 FROM users WHERE username=?", (r["username"],)).fetchone():
            return r["username"]
    return None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "Touchline/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # helpers
    def client_ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def token(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def user(self):
        return session_user(self.token())

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to, headers=None):
        h = {"Location": to}
        h.update(headers or {})
        self.send(303, b"", "text/plain", h)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            raise ValueError("too large")
        return self.rfile.read(n)

    def static(self, name, ctype="text/html; charset=utf-8"):
        with open(os.path.join(STATIC, name), "rb") as f:
            self.send(200, f.read(), ctype)

    def cookie_header(self, value, max_age):
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    # routes
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if path == "/login":
            return self.static("login.html")
        if path == "/core.js":
            return self.static("core.js", "text/javascript; charset=utf-8")
        if path == "/sw.js":
            return self.static("sw.js", "text/javascript; charset=utf-8")
        if path in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png", "/favicon.ico"):
            return self.static("icon-192.png" if path == "/favicon.ico" else path[1:], "image/png")
        if path.startswith("/s/"):
            with db() as c:
                ok = c.execute("SELECT 1 FROM shares WHERE token=?", (path[3:],)).fetchone()
            return self.static("share.html") if ok else self.send(404, "This link is not active.", "text/plain; charset=utf-8")
        if path.startswith("/api/public/"):
            return self.public(path[len("/api/public/"):])
        if path in ("/manifest.webmanifest",):
            return self.send(200, {
                "id": "/", "name": "Touchline Tracker", "short_name": "Touchline",
                "description": "Track kids' 5-a-side matches: goals, subs, minutes and keepers.",
                "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
                "background_color": "#1f6b3a", "theme_color": "#1f6b3a",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                    {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]},
                "application/manifest+json")
        user = self.user()
        if path == "/":
            return self.static("index.html") if user else self.redirect("/login")
        if not user:
            return self.send(401, {"error": "Sign in required"})
        if path == "/api/me":
            return self.send(200, {"username": user})
        if path == "/api/state":
            devices = touch(user, self.token())
            known = (parse_qs(urlparse(self.path).query).get("v") or [None])[0]
            with db() as c:
                r = c.execute("SELECT data, version FROM state WHERE username=?", (user,)).fetchone()
            version = r["version"] if r else 0
            if known is not None and known == str(version):
                return self.send(200, {"version": version, "devices": devices})  # nothing new
            return self.send(200, {"data": json.loads(r["data"]) if r else None, "version": version, "devices": devices})
        if path == "/api/share":
            with db() as c:
                r = c.execute("SELECT token FROM shares WHERE username=?", (user,)).fetchone()
            return self.send(200, {"token": r["token"] if r else None})
        if path == "/api/export":
            with db() as c:
                r = c.execute("SELECT data FROM state WHERE username=?", (user,)).fetchone()
            name = f"touchline-{user}-{time.strftime('%Y-%m-%d')}.json"
            return self.send(200, r["data"] if r else "{}", "application/json",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            ip = self.client_ip()
            cnt, first = _failed.get(ip, [0, time.time()])
            if time.time() - first > 900:
                cnt, first = 0, time.time()
            if cnt >= 10:
                return self.redirect("/login?e=locked")
            try:
                form = parse_qs(self.body().decode())
            except Exception:
                return self.redirect("/login?e=1")
            u = (form.get("username") or [""])[0].strip()
            p = (form.get("password") or [""])[0]
            with db() as c:
                r = c.execute("SELECT pw_hash FROM users WHERE username=?", (u,)).fetchone()
            if r and check_pw(p, r["pw_hash"]):
                _failed.pop(ip, None)
                t = new_session(u)
                return self.redirect("/", {"Set-Cookie": self.cookie_header(t, SESSION_DAYS * 86400)})
            _failed[ip] = [cnt + 1, first]
            time.sleep(1)
            return self.redirect("/login?e=1")
        if path == "/api/share":
            user = self.user()
            if not user:
                return self.send(401, {"error": "Sign in required"})
            t = secrets.token_urlsafe(18)
            with db() as c:
                c.execute("DELETE FROM shares WHERE username=?", (user,))
                c.execute("INSERT INTO shares VALUES(?,?,?)", (t, user, int(time.time())))
            return self.send(200, {"token": t})
        if path == "/logout":
            t = self.token()
            if t:
                with db() as c:
                    c.execute("DELETE FROM sessions WHERE token_hash=?", (tok_hash(t),))
            return self.redirect("/login", {"Set-Cookie": self.cookie_header("", 0)})
        self.send(404, {"error": "Not found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        user = self.user()
        if not user:
            return self.send(401, {"error": "Sign in required"})
        if path != "/api/state":
            return self.send(404, {"error": "Not found"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send(415, {"error": "JSON required"})
        try:
            payload = json.loads(self.body())
            data = payload["data"]
            assert isinstance(data, dict)
        except Exception:
            return self.send(400, {"error": "Bad data"})
        now = int(time.time())
        devices = touch(user, self.token())
        with db() as c:
            r = c.execute("SELECT version, data FROM state WHERE username=?", (user,)).fetchone()
            current = r["version"] if r else 0
            base = payload.get("base")
            if base is not None and r and int(base) != current:
                # another phone saved in between: send its data back so this phone can merge and retry
                return self.send(409, {"data": json.loads(r["data"]), "version": current, "devices": devices})
            version = current + 1
            c.execute("INSERT INTO state VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                      "data=excluded.data, version=excluded.version, updated_at=excluded.updated_at",
                      (user, json.dumps(data), version, now))
            # keep a queryable copy of every finished match
            squad = {p.get("id"): p for p in data.get("squad", [])}
            for h in data.get("history", []):
                if not h.get("date"):
                    continue
                rec = dict(h)
                rec["players"] = {(squad.get(pid) or {}).get("name", pid): v
                                  for pid, v in (h.get("players") or {}).items()}
                c.execute("INSERT INTO matches VALUES(?,?,?,?,?,?) ON CONFLICT(username, match_date) DO UPDATE SET "
                          "opponent=excluded.opponent, goals_for=excluded.goals_for, "
                          "goals_against=excluded.goals_against, data=excluded.data",
                          (user, h["date"], h.get("opp"), h.get("us"), h.get("them"), json.dumps(rec)))
        self.send(200, {"ok": True, "version": version, "devices": devices})

    def do_DELETE(self):
        user = self.user()
        if not user:
            return self.send(401, {"error": "Sign in required"})
        if urlparse(self.path).path == "/api/share":
            with db() as c:
                c.execute("DELETE FROM shares WHERE username=?", (user,))
            return self.send(200, {"ok": True})
        self.send(404, {"error": "Not found"})

    def public(self, token):
        """Read-only data for the parent link: names, current match, recent results. No account data."""
        with db() as c:
            s = c.execute("SELECT username FROM shares WHERE token=?", (token,)).fetchone()
            r = c.execute("SELECT data FROM state WHERE username=?", (s["username"],)).fetchone() if s else None
        if not s:
            return self.send(404, {"error": "Link not active"})
        data = json.loads(r["data"]) if r else {}
        settings = data.get("settings") or {}
        match = data.get("match") or {}
        out = {
            "teamName": settings.get("teamName") or "Us",
            "matchMin": settings.get("matchMin") or 12,
            "squad": [{"id": p.get("id"), "name": p.get("name"), "num": p.get("num")} for p in data.get("squad", [])],
            "match": {k: match.get(k) for k in ("id", "day", "opp", "clock", "events", "removed")},
            "history": [{k: h.get(k) for k in ("id", "date", "opp", "us", "them", "players", "timeline")}
                        for h in (data.get("history") or [])[:8]],
        }
        out["match"]["events"] = out["match"]["events"] or []
        out["match"]["removed"] = out["match"]["removed"] or []
        out["match"]["clock"] = out["match"]["clock"] or {}
        self.send(200, out)


# ---------- CLI ----------
def ask_pw():
    p1 = getpass.getpass("Password: ")
    p2 = getpass.getpass("Repeat password: ")
    if p1 != p2 or len(p1) < 8:
        sys.exit("Passwords must match and be at least 8 characters.")
    return p1


def cli(args):
    init_db()
    cmd = args[0]
    if cmd == "users":
        with db() as c:
            for r in c.execute("SELECT username, created_at FROM users ORDER BY username"):
                print(r["username"], time.strftime("%Y-%m-%d", time.localtime(r["created_at"])))
        return
    if len(args) < 2:
        sys.exit(__doc__)
    if cmd == "import-sqlite":
        return import_sqlite(args[1])
    name = args[1]
    with db() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone()
        if cmd == "adduser":
            if exists:
                sys.exit(f"'{name}' already exists.")
            c.execute("INSERT INTO users VALUES(?,?,?)", (name, hash_pw(ask_pw()), int(time.time())))
            print(f"Created '{name}'.")
        elif cmd == "passwd":
            if not exists:
                sys.exit(f"No account '{name}'.")
            c.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(ask_pw()), name))
            c.execute("DELETE FROM sessions WHERE username=?", (name,))
            print(f"Password changed; '{name}' is signed out everywhere.")
        elif cmd == "deluser":
            for t in TABLES:
                c.execute(f"DELETE FROM {t} WHERE username=?", (name,))
            print(f"Deleted '{name}' and their data.")
        else:
            sys.exit(__doc__)


def import_sqlite(path):
    """Copy all rows from an old SQLite file into the current (PostgreSQL) database. Safe to re-run."""
    if not USE_PG:
        sys.exit("Set DATABASE_URL to your PostgreSQL database first; this copies SQLite into it.")
    if not os.path.exists(path):
        sys.exit(f"No file at {path}")
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    keys = {"users": "username", "sessions": "token_hash", "state": "username",
            "shares": "token", "matches": "username, match_date"}
    total = 0
    with db() as c:
        for t in TABLES:
            try:
                rows = src.execute(f"SELECT * FROM {t}").fetchall()
            except sqlite3.OperationalError:
                continue  # older file without this table
            for r in rows:
                cols = list(r.keys())
                c.execute(f"INSERT INTO {t}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
                          f"ON CONFLICT({keys[t]}) DO NOTHING", tuple(r))
            print(f"{t}: {len(rows)} rows")
            total += len(rows)
    print(f"Done. Checked {total} rows; new ones were copied and rows already in PostgreSQL were left as they are.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "serve":
        cli(sys.argv[1:])
    else:
        init_db()
        where = "PostgreSQL" if USE_PG else f"SQLite {DB_PATH}"
        print(f"Touchline Tracker on http://0.0.0.0:{PORT}  (database: {where})")
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
