#!/usr/bin/env python3
"""Touchline Tracker server: app pages, login, teams with roles, and database storage.

Database: PostgreSQL when DATABASE_URL is set (recommended, see docker-compose.yml),
otherwise a local SQLite file (DB_PATH). SQLite needs nothing extra; PostgreSQL needs psycopg.

Roles per team: admin (everything), tracker (live match), parent (read only, own child), observer (read only).

Usage:
  python app.py                          start the server (default)
  python app.py adduser NAME [TEAM NAME] create an account and a team they run as admin (asks for a password)
  python app.py passwd NAME              change a password
  python app.py deluser NAME             delete an account (teams and their data stay)
  python app.py users                    list accounts and their teams
  python app.py teams                    list teams and members
  python app.py addmember NAME TEAM_ID ROLE   give an existing account a role on a team
  python app.py import-sqlite FILE       copy an old SQLite database into PostgreSQL
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

import store
from store import ROLES, CAN_TRACK, USERNAME_RE

DB_PATH = os.environ.get("DB_PATH", "data/touchline.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
PORT = int(os.environ.get("PORT", "8080"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"  # set to 1 when served over HTTPS
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "90"))
INVITE_HOURS = int(os.environ.get("INVITE_HOURS", "48"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 5 * 1024 * 1024
PBKDF2_ITER = 600_000
COOKIE = "tt_session"
VERSION = "2026.10.18-roles"

_failed = {}  # ip -> [count, first_ts] login throttle
_seen = {}    # team id -> {session hash: last seen}, shows how many phones are live


def touch(team_id, token):
    t = time.time()
    d = _seen.setdefault(team_id, {})
    d[tok_hash(token or "")] = t
    for k in [k for k, v in d.items() if t - v > 20]:
        d.pop(k, None)
    return len(d)


# ---------- database ----------
class PgConn:
    """Lets PostgreSQL be used like sqlite3 everywhere else ('?' placeholders, rows by column name, commit on exit)."""

    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.rollback() if exc_type else self.conn.commit()
        finally:
            self.conn.close()

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)

    def executescript(self, script):
        for stmt in [x.strip() for x in script.split(";") if x.strip()]:
            self.conn.execute(stmt)


class SqliteConn:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, timeout=15)
        self.conn.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.rollback() if exc_type else self.conn.commit()
        finally:
            self.conn.close()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executescript(self, script):
        self.conn.executescript(script)


def db():
    return PgConn() if USE_PG else SqliteConn()


def table_exists(name):
    try:
        with db() as c:
            c.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def wait_for_db(seconds=60):
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
    # Earlier versions kept one JSON blob per account. Rename those tables, then convert them into teams.
    legacy = table_exists("state") and not table_exists("teams")
    if legacy:
        old_shares = table_exists("shares")
        old_matches = table_exists("matches")
        with db() as c:
            c.execute("ALTER TABLE state RENAME TO legacy_state")
            if old_shares:
                c.execute("ALTER TABLE shares RENAME TO legacy_shares")
            if old_matches:
                c.execute("ALTER TABLE matches RENAME TO legacy_matches")
    with db() as c:
        c.executescript(store.SCHEMA)
    if legacy:
        convert_legacy()
    u, p = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
    if u and p:
        with db() as c:
            if not c.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
                c.execute("INSERT INTO users VALUES(?,?,?)", (u, hash_pw(p), store.now()))
                store.create_team(c, "My team", u)
                print(f"Created account '{u}' with a new team, from ADMIN_USER")


def convert_legacy():
    shares = {}
    if table_exists("legacy_shares"):
        with db() as c:
            shares = {r["username"]: r["token"] for r in c.execute("SELECT username, token FROM legacy_shares").fetchall()}
    with db() as c:
        rows = c.execute("SELECT username, data FROM legacy_state").fetchall()
        for r in rows:
            store.team_from_blob(c, r["username"], json.loads(r["data"]), shares.get(r["username"]))
        # accounts that never saved anything still get their own team
        for u in c.execute("SELECT username FROM users WHERE username NOT IN (SELECT username FROM memberships)").fetchall():
            store.create_team(c, "My team", u["username"])
    print(f"Upgraded {len(rows)} account(s) to teams with roles. Old tables kept as legacy_*.")


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
        c.execute("DELETE FROM sessions WHERE expires<?", (store.now(),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok_hash(t), username, store.now() + SESSION_DAYS * 86400))
    return t


def session_user(token):
    if not token:
        return None
    with db() as c:
        r = c.execute("SELECT s.username FROM sessions s JOIN users u ON u.username=s.username "
                      "WHERE s.token_hash=? AND s.expires>?", (tok_hash(token), store.now())).fetchone()
    return r["username"] if r else None


def valid_password(p):
    return isinstance(p, str) and 8 <= len(p) <= 200


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "Touchline/2"

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
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            raise HttpError(413, "Too large")
        return self.rfile.read(n)

    def json_body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise HttpError(415, "JSON required")
        try:
            d = json.loads(self.body() or b"{}")
        except ValueError:
            raise HttpError(400, "Bad data")
        if not isinstance(d, dict):
            raise HttpError(400, "Bad data")
        return d

    def static(self, name, ctype="text/html; charset=utf-8"):
        with open(os.path.join(STATIC, name), "rb") as f:
            self.send(200, f.read(), ctype)

    def cookie_header(self, value, max_age):
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def same_origin(self):
        """Extra protection for changes: a request from another website is refused."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def need_user(self):
        u = self.user()
        if not u:
            raise HttpError(401, "Sign in required")
        return u

    def team(self, user, roles=None):
        """The team this request is for (X-Team header), with the user's role on it."""
        want = self.headers.get("X-Team") or (parse_qs(urlparse(self.path).query).get("team") or [None])[0]
        with db() as c:
            ms = store.memberships(c, user)
        m = next((x for x in ms if x["id"] == want), None) if want else (ms[0] if ms else None)
        if not m:
            raise HttpError(403, "You are not in this team")
        if roles and m["role"] not in roles:
            raise HttpError(403, "Your role can't do this")
        return m

    def run(self, fn):
        try:
            fn()
        except HttpError as e:
            self.send(e.code, {"error": e.msg})
        except BrokenPipeError:
            pass

    def do_GET(self):
        self.run(self.get)

    def do_POST(self):
        self.run(self.post)

    def do_PUT(self):
        self.run(self.put)

    def do_PATCH(self):
        self.run(self.patch)

    def do_DELETE(self):
        self.run(self.delete)

    # ----- GET -----
    def get(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True, "version": VERSION, "database": "postgresql" if USE_PG else "sqlite"})
        if path == "/login":
            return self.static("login.html")
        if path in ("/core.js", "/sw.js"):
            return self.static(path[1:], "text/javascript; charset=utf-8")
        if path in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png", "/favicon.ico"):
            return self.static("icon-192.png" if path == "/favicon.ico" else path[1:], "image/png")
        if path == "/manifest.webmanifest":
            return self.send(200, MANIFEST, "application/manifest+json")
        if path.startswith("/s/"):
            with db() as c:
                ok = c.execute("SELECT 1 FROM shares WHERE token=?", (path[3:],)).fetchone()
            return self.static("share.html") if ok else self.send(404, "This link is not active.", "text/plain; charset=utf-8")
        if path.startswith("/api/public/"):
            return self.public(path[len("/api/public/"):])
        if path.startswith("/join/") or path.startswith("/reset/"):
            return self.static("join.html")
        if path.startswith("/api/invite/"):
            return self.invite_info(path[len("/api/invite/"):])
        user = self.user()
        if path == "/":
            return self.static("index.html") if user else self.redirect("/login")
        user = self.need_user()
        if path == "/api/me":
            with db() as c:
                return self.send(200, {"username": user, "teams": store.memberships(c, user)})
        if path == "/api/state":
            m = self.team(user)
            devices = touch(m["id"], self.token())
            known = (parse_qs(urlparse(self.path).query).get("v") or [None])[0]
            with db() as c:
                v = c.execute("SELECT version FROM teams WHERE id=?", (m["id"],)).fetchone()["version"]
                if known is not None and known == str(v):
                    return self.send(200, {"version": v, "devices": devices, "role": m["role"]})
                data, v = store.assemble(c, m["id"], m["role"], m["playerId"])
            return self.send(200, {"data": data, "version": v, "devices": devices, "role": m["role"]})
        if path == "/api/export":
            m = self.team(user)
            with db() as c:
                data, _ = store.assemble(c, m["id"], m["role"], m["playerId"])
            name = f"touchline-{m['name']}-{time.strftime('%Y-%m-%d')}.json".replace(" ", "_")
            return self.send(200, json.dumps(data, indent=1), "application/json",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/api/share":
            m = self.team(user, ("admin",))
            with db() as c:
                r = c.execute("SELECT token FROM shares WHERE team_id=?", (m["id"],)).fetchone()
            return self.send(200, {"token": r["token"] if r else None})
        if path == "/api/members":
            m = self.team(user, ("admin",))
            return self.send(200, self.members(m["id"], user))
        if path == "/api/activity":
            m = self.team(user, ("admin",))
            with db() as c:
                return self.send(200, {"items": store.activity(c, m["id"])})
        self.send(404, {"error": "Not found"})

    def members(self, tid, me):
        with db() as c:
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM players WHERE team_id=?", (tid,)).fetchall()}
            mem = [{"username": r["username"], "role": r["role"], "playerId": r["player_id"],
                    "playerName": names.get(r["player_id"]), "you": r["username"] == me}
                   for r in c.execute("SELECT username, role, player_id FROM memberships WHERE team_id=? ORDER BY created_at", (tid,)).fetchall()]
            inv = [{"id": r["id"], "kind": r["kind"], "role": r["role"], "playerName": names.get(r["player_id"]),
                    "username": r["username"], "expires": r["expires"], "createdBy": r["created_by"]}
                   for r in c.execute("SELECT id, kind, role, player_id, username, expires, created_by FROM invites "
                                      "WHERE team_id=? AND used_at IS NULL AND expires>? ORDER BY created_at DESC", (tid, store.now())).fetchall()]
        return {"members": mem, "invites": inv}

    def invite_row(self, token):
        with db() as c:
            r = c.execute("SELECT i.*, t.name AS team_name FROM invites i JOIN teams t ON t.id=i.team_id WHERE i.token_hash=?",
                          (tok_hash(token),)).fetchone()
        if not r or r["used_at"] or r["expires"] < store.now():
            raise HttpError(404, "This link has expired or was already used. Ask for a new one.")
        return r

    def invite_info(self, token):
        r = self.invite_row(token)
        with db() as c:
            p = c.execute("SELECT name FROM players WHERE id=?", (r["player_id"],)).fetchone() if r["player_id"] else None
        self.send(200, {"kind": r["kind"], "team": r["team_name"], "role": r["role"], "player": p["name"] if p else None,
                        "username": r["username"] if r["kind"] == "reset" else None, "signedIn": self.user()})

    # ----- POST -----
    def post(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self.login()
        if path == "/logout":
            t = self.token()
            if t:
                with db() as c:
                    c.execute("DELETE FROM sessions WHERE token_hash=?", (tok_hash(t),))
            return self.redirect("/login", {"Set-Cookie": self.cookie_header("", 0)})
        if not self.same_origin():
            raise HttpError(403, "Blocked")
        if path.startswith("/api/invite/"):
            return self.accept_invite(path[len("/api/invite/"):])
        user = self.need_user()
        if path == "/api/share":
            m = self.team(user, ("admin",))
            t = secrets.token_urlsafe(18)
            with db() as c:
                c.execute("DELETE FROM shares WHERE team_id=?", (m["id"],))
                c.execute("INSERT INTO shares VALUES(?,?,?)", (t, m["id"], store.now()))
                store.log(c, m["id"], user, "sharing", "made a new parent link")
            return self.send(200, {"token": t})
        if path == "/api/invites":
            m = self.team(user, ("admin",))
            d = self.json_body()
            role = d.get("role")
            if role not in ROLES:
                raise HttpError(400, "Pick a role")
            pid = d.get("playerId") if role == "parent" else None
            with db() as c:
                if role == "parent" and not c.execute("SELECT 1 FROM players WHERE id=? AND team_id=? AND deleted=0", (pid, m["id"])).fetchone():
                    raise HttpError(400, "Pick the parent's child")
                t, iid = self.make_invite(c, m["id"], "join", user, role=role, player_id=pid)
                store.log(c, m["id"], user, "people", f"invited a new {role}")
            return self.send(200, {"url": f"/join/{t}", "id": iid})
        if path.startswith("/api/members/") and path.endswith("/reset"):
            m = self.team(user, ("admin",))
            target = path[len("/api/members/"):-len("/reset")]
            with db() as c:
                theirs = store.memberships(c, target)
                mine = {x["id"] for x in store.memberships(c, user) if x["role"] == "admin"}
                if not theirs or any(x["id"] not in mine for x in theirs):
                    raise HttpError(403, "You can only reset passwords for people who are only in teams you run.")
                t, iid = self.make_invite(c, m["id"], "reset", user, username=target)
                store.log(c, m["id"], user, "people", f"made a password reset link for {target}")
            return self.send(200, {"url": f"/reset/{t}"})
        if path == "/api/teams":
            d = self.json_body()
            name = store.clip(d.get("name") or "New team", 60).strip() or "New team"
            with db() as c:
                tid = store.create_team(c, name, user)
            return self.send(200, {"id": tid, "name": name})
        if path == "/api/password":
            d = self.json_body()
            with db() as c:
                r = c.execute("SELECT pw_hash FROM users WHERE username=?", (user,)).fetchone()
                if not check_pw(d.get("current") or "", r["pw_hash"]):
                    time.sleep(1)
                    raise HttpError(400, "Your current password is wrong.")
                if not valid_password(d.get("new")):
                    raise HttpError(400, "The new password needs at least 8 characters.")
                c.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(d["new"]), user))
                c.execute("DELETE FROM sessions WHERE username=? AND token_hash<>?", (user, tok_hash(self.token() or "")))
            return self.send(200, {"ok": True})
        self.send(404, {"error": "Not found"})

    def make_invite(self, c, tid, kind, by, role=None, player_id=None, username=None):
        t = secrets.token_urlsafe(24)
        iid = store.nid()
        c.execute("INSERT INTO invites VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                  (tok_hash(t), iid, tid, kind, role, player_id, username, by, store.now(), store.now() + INVITE_HOURS * 3600))
        return t, iid

    def login(self):
        ip = self.client_ip()
        cnt, first = _failed.get(ip, [0, time.time()])
        if time.time() - first > 900:
            cnt, first = 0, time.time()
        if cnt >= 10:
            return self.redirect("/login?e=locked")
        form = parse_qs(self.body().decode(errors="replace"))
        u = (form.get("username") or [""])[0].strip()
        p = (form.get("password") or [""])[0]
        with db() as c:
            r = c.execute("SELECT pw_hash FROM users WHERE username=?", (u,)).fetchone()
        if r and check_pw(p, r["pw_hash"]):
            _failed.pop(ip, None)
            return self.redirect("/", {"Set-Cookie": self.cookie_header(new_session(u), SESSION_DAYS * 86400)})
        _failed[ip] = [cnt + 1, first]
        time.sleep(1)
        return self.redirect("/login?e=1")

    def accept_invite(self, token):
        r = self.invite_row(token)
        d = self.json_body()
        with db() as c:
            if r["kind"] == "reset":
                if not valid_password(d.get("password")):
                    raise HttpError(400, "The password needs at least 8 characters.")
                c.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(d["password"]), r["username"]))
                c.execute("DELETE FROM sessions WHERE username=?", (r["username"],))
                c.execute("UPDATE invites SET used_at=?, used_by=? WHERE token_hash=?", (store.now(), r["username"], r["token_hash"]))
                store.log(c, r["team_id"], r["username"], "people", "set a new password from a reset link")
                username = r["username"]
            else:
                mode = d.get("mode")
                if mode == "current":
                    username = self.need_user()
                elif mode == "new":
                    username = str(d.get("username") or "").strip()
                    if not USERNAME_RE.match(username):
                        raise HttpError(400, "Usernames are 3 to 32 letters, numbers, dots, dashes or underscores.")
                    if not valid_password(d.get("password")):
                        raise HttpError(400, "The password needs at least 8 characters.")
                    if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                        raise HttpError(400, "That username is taken. Pick another, or sign in if it's yours.")
                    c.execute("INSERT INTO users VALUES(?,?,?)", (username, hash_pw(d["password"]), store.now()))
                elif mode == "existing":
                    username = str(d.get("username") or "").strip()
                    u = c.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
                    if not u or not check_pw(d.get("password") or "", u["pw_hash"]):
                        time.sleep(1)
                        raise HttpError(400, "Wrong username or password.")
                else:
                    raise HttpError(400, "Bad request")
                c.execute("INSERT INTO memberships VALUES(?,?,?,?,?) ON CONFLICT(team_id, username) DO UPDATE SET "
                          "role=excluded.role, player_id=excluded.player_id",
                          (r["team_id"], username, r["role"], r["player_id"], store.now()))
                c.execute("UPDATE invites SET used_at=?, used_by=? WHERE token_hash=?", (store.now(), username, r["token_hash"]))
                store.log(c, r["team_id"], username, "people", f"joined as {r['role']}")
        t = new_session(username)
        self.send(200, {"ok": True, "team": r["team_id"]}, headers={"Set-Cookie": self.cookie_header(t, SESSION_DAYS * 86400)})

    # ----- PUT: the phone's changes to the team's data -----
    def put(self):
        if urlparse(self.path).path != "/api/state":
            return self.send(404, {"error": "Not found"})
        if not self.same_origin():
            raise HttpError(403, "Blocked")
        user = self.need_user()
        m = self.team(user)
        if m["role"] not in CAN_TRACK:
            raise HttpError(403, "Your role is view only")
        payload = self.json_body()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HttpError(400, "Bad data")
        devices = touch(m["id"], self.token())
        with db() as c:
            base = payload.get("base")
            cas = c.execute("UPDATE teams SET version=version+1 WHERE id=? AND version=?", (m["id"], store.as_int(base, -1))) if base is not None \
                else c.execute("UPDATE teams SET version=version+1 WHERE id=?", (m["id"],))
            if cas.rowcount == 0:
                cur, v = store.assemble(c, m["id"], m["role"], m["playerId"])
                return self.send(409, {"data": cur, "version": v, "devices": devices, "role": m["role"]})
            dropped = store.apply(c, m["id"], user, m["role"], data)
            v = c.execute("SELECT version FROM teams WHERE id=?", (m["id"],)).fetchone()["version"]
            out = {"ok": True, "version": v, "devices": devices, "role": m["role"]}
            if dropped:
                out["data"], _ = store.assemble(c, m["id"], m["role"], m["playerId"])
                out["note"] = "Some changes need an admin and were not saved."
        self.send(200, out)

    # ----- PATCH / DELETE: people and links -----
    def patch(self):
        path = urlparse(self.path).path
        if not self.same_origin():
            raise HttpError(403, "Blocked")
        user = self.need_user()
        if not path.startswith("/api/members/"):
            return self.send(404, {"error": "Not found"})
        m = self.team(user, ("admin",))
        target = path[len("/api/members/"):]
        d = self.json_body()
        role = d.get("role")
        if role not in ROLES:
            raise HttpError(400, "Pick a role")
        with db() as c:
            r = c.execute("SELECT role FROM memberships WHERE team_id=? AND username=?", (m["id"], target)).fetchone()
            if not r:
                raise HttpError(404, "Not in this team")
            if r["role"] == "admin" and role != "admin" and store.admin_count(c, m["id"]) <= 1:
                raise HttpError(400, "A team needs at least one admin. Make someone else admin first.")
            pid = d.get("playerId") if role == "parent" else None
            if role == "parent" and not c.execute("SELECT 1 FROM players WHERE id=? AND team_id=? AND deleted=0", (pid, m["id"])).fetchone():
                raise HttpError(400, "Pick the parent's child")
            c.execute("UPDATE memberships SET role=?, player_id=? WHERE team_id=? AND username=?", (role, pid, m["id"], target))
            store.log(c, m["id"], user, "people", f"changed {target} to {role}")
        self.send(200, self.members(m["id"], user))

    def delete(self):
        path = urlparse(self.path).path
        if not self.same_origin():
            raise HttpError(403, "Blocked")
        user = self.need_user()
        if path == "/api/share":
            m = self.team(user, ("admin",))
            with db() as c:
                c.execute("DELETE FROM shares WHERE team_id=?", (m["id"],))
                store.log(c, m["id"], user, "sharing", "turned off the parent link")
            return self.send(200, {"ok": True})
        if path.startswith("/api/invites/"):
            m = self.team(user, ("admin",))
            with db() as c:
                c.execute("DELETE FROM invites WHERE id=? AND team_id=? AND used_at IS NULL", (path[len("/api/invites/"):], m["id"]))
            return self.send(200, self.members(m["id"], user))
        if path.startswith("/api/members/"):
            m = self.team(user, ("admin",))
            target = path[len("/api/members/"):]
            with db() as c:
                r = c.execute("SELECT role FROM memberships WHERE team_id=? AND username=?", (m["id"], target)).fetchone()
                if not r:
                    raise HttpError(404, "Not in this team")
                if r["role"] == "admin" and store.admin_count(c, m["id"]) <= 1:
                    raise HttpError(400, "A team needs at least one admin.")
                c.execute("DELETE FROM memberships WHERE team_id=? AND username=?", (m["id"], target))
                store.log(c, m["id"], user, "people", f"removed {target} from the team")
            return self.send(200, self.members(m["id"], user))
        self.send(404, {"error": "Not found"})

    def public(self, token):
        """Read-only data for the parent link: names, current match, recent results."""
        with db() as c:
            s = c.execute("SELECT team_id FROM shares WHERE token=?", (token,)).fetchone()
            if not s:
                return self.send(404, {"error": "Link not active"})
            data, _ = store.assemble(c, s["team_id"], "observer")
        match = data.get("match") or {}
        out = {
            "teamName": data["settings"].get("teamName") or "Us",
            "matchMin": data["settings"].get("matchMin") or 12,
            "squad": [{"id": p["id"], "name": p["name"], "num": p["num"]} for p in data["squad"]],
            "match": {"id": match.get("id"), "day": match.get("day"), "opp": match.get("opp"), "clock": match.get("clock") or {},
                      "events": [{k: e[k] for k in ("id", "t", "n", "type", "pid")} for e in match.get("events") or []],
                      "removed": match.get("removed") or []},
            "history": [{k: h.get(k) for k in ("id", "date", "opp", "us", "them", "players", "timeline")} for h in data["history"][:8]],
        }
        self.send(200, out)


MANIFEST = {
    "id": "/", "name": "Touchline Tracker", "short_name": "Touchline",
    "description": "Track kids' 5-a-side matches: goals, subs, minutes and keepers.",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#1f6b3a", "theme_color": "#1f6b3a",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}


# ---------- CLI ----------
def ask_pw():
    p1 = getpass.getpass("Password: ")
    p2 = getpass.getpass("Repeat password: ")
    if p1 != p2 or not valid_password(p1):
        sys.exit("Passwords must match and be at least 8 characters.")
    return p1


def cli(args):
    init_db()
    cmd = args[0]
    if cmd in ("users", "teams"):
        with db() as c:
            if cmd == "users":
                for r in c.execute("SELECT username FROM users ORDER BY username").fetchall():
                    ms = store.memberships(c, r["username"])
                    print(r["username"], "·", ", ".join(f"{m['name']} ({m['role']})" for m in ms) or "no team")
            else:
                for t in c.execute("SELECT id, name FROM teams ORDER BY created_at").fetchall():
                    mem = c.execute("SELECT username, role FROM memberships WHERE team_id=?", (t["id"],)).fetchall()
                    print(f"{t['id']}  {t['name']}: " + ", ".join(f"{m['username']} ({m['role']})" for m in mem))
        return
    if cmd == "import-sqlite" and len(args) > 1:
        return import_sqlite(args[1])
    if len(args) < 2:
        sys.exit(__doc__)
    name = args[1]
    with db() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone()
        if cmd == "adduser":
            if exists:
                sys.exit(f"'{name}' already exists.")
            if not USERNAME_RE.match(name):
                sys.exit("Usernames are 3 to 32 letters, numbers, dots, dashes or underscores.")
            c.execute("INSERT INTO users VALUES(?,?,?)", (name, hash_pw(ask_pw()), store.now()))
            team = " ".join(args[2:]) or "My team"
            tid = store.create_team(c, team, name)
            print(f"Created '{name}', admin of team '{team}' ({tid}).")
        elif cmd == "passwd":
            if not exists:
                sys.exit(f"No account '{name}'.")
            c.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(ask_pw()), name))
            c.execute("DELETE FROM sessions WHERE username=?", (name,))
            print(f"Password changed; '{name}' is signed out everywhere.")
        elif cmd == "deluser":
            for t in ("users", "sessions", "memberships"):
                c.execute(f"DELETE FROM {t} WHERE username=?", (name,))
            print(f"Deleted '{name}'. Their teams and data stay.")
        elif cmd == "addmember" and len(args) == 4:
            if not exists or args[3] not in ROLES:
                sys.exit("Usage: addmember NAME TEAM_ID admin|tracker|parent|observer")
            c.execute("INSERT INTO memberships VALUES(?,?,?,NULL,?) ON CONFLICT(team_id, username) DO UPDATE SET role=excluded.role",
                      (args[2], name, args[3], store.now()))
            print(f"'{name}' is now {args[3]} of team {args[2]}.")
        else:
            sys.exit(__doc__)


def import_sqlite(path):
    """Copy an old SQLite database (any version) into PostgreSQL. Safe to re-run: existing rows are kept."""
    if not USE_PG:
        sys.exit("Set DATABASE_URL to your PostgreSQL database first; this copies SQLite into it.")
    if not os.path.exists(path):
        sys.exit(f"No file at {path}")
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with db() as c:
        for r in src.execute("SELECT * FROM users").fetchall():
            c.execute("INSERT INTO users VALUES(?,?,?) ON CONFLICT(username) DO NOTHING", (r["username"], r["pw_hash"], r["created_at"]))
        if "teams" in tables:  # current format: copy table by table
            for t in ("teams", "memberships", "players", "opponents", "games", "live", "events", "invites", "shares", "activity"):
                if t not in tables:
                    continue
                for r in src.execute(f"SELECT * FROM {t}").fetchall():
                    cols = list(r.keys())
                    c.execute(f"INSERT INTO {t}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) ON CONFLICT DO NOTHING", tuple(r))
                print(f"{t}: copied")
        else:  # first versions: one JSON blob per account
            shares = {r["username"]: r["token"] for r in src.execute("SELECT username, token FROM shares")} if "shares" in tables else {}
            done = {r["username"] for r in c.execute("SELECT DISTINCT username FROM memberships").fetchall()}
            n = 0
            for r in src.execute("SELECT username, data FROM state").fetchall() if "state" in tables else []:
                if r["username"] in done:
                    print(f"skipped {r['username']}: already has a team here")
                    continue
                store.team_from_blob(c, r["username"], json.loads(r["data"]), shares.get(r["username"]))
                n += 1
            print(f"converted {n} account(s) into teams")
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "serve":
        cli(sys.argv[1:])
    else:
        init_db()
        where = "PostgreSQL" if USE_PG else f"SQLite {DB_PATH}"
        print(f"Touchline Tracker {VERSION} on http://0.0.0.0:{PORT}  (database: {where})")
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
