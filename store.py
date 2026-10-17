"""Team data: tables, per-role views and per-role change rules.

The phone works with one JSON "state" per team (squad, opponents, history, live match, settings).
The server keeps that data in proper tables and decides, per role, what each person may see and change:

  admin     everything, including people, squad, settings, plans and past matches
  tracker   live match only: events, clock, opponent, saving or resetting a match
  parent    read only; sees team results and only their own child's stats
  observer  read only; sees everything
"""
import json
import re
import secrets
import time

ROLES = ("admin", "tracker", "parent", "observer")
CAN_TRACK = ("admin", "tracker")
EVENT_TYPES = {"kickoff", "pause", "resume", "goal", "assist", "save", "shot", "on", "off", "against", "gk"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS teams(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, settings TEXT NOT NULL, version BIGINT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships(
  team_id TEXT NOT NULL, username TEXT NOT NULL, role TEXT NOT NULL, player_id TEXT, created_at BIGINT NOT NULL,
  PRIMARY KEY(team_id, username));
CREATE TABLE IF NOT EXISTS players(
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL, num TEXT, no_gk INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS opponents(
  team_id TEXT NOT NULL, norm TEXT NOT NULL, name TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(team_id, norm));
CREATE TABLE IF NOT EXISTS games(
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, played_at TEXT NOT NULL, opponent TEXT, goals_for INTEGER,
  goals_against INTEGER, len_ms BIGINT, players TEXT, timeline TEXT, summary TEXT, saved_by TEXT,
  deleted INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS live(
  team_id TEXT PRIMARY KEY, match TEXT NOT NULL, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, match_id TEXT NOT NULL, t BIGINT NOT NULL, n BIGINT NOT NULL,
  type TEXT NOT NULL, player_id TEXT, grp TEXT, created_by TEXT, created_at BIGINT NOT NULL,
  removed INTEGER NOT NULL DEFAULT 0, removed_by TEXT);
CREATE INDEX IF NOT EXISTS events_match ON events(team_id, match_id);
CREATE TABLE IF NOT EXISTS invites(
  token_hash TEXT PRIMARY KEY, id TEXT NOT NULL, team_id TEXT NOT NULL, kind TEXT NOT NULL, role TEXT,
  player_id TEXT, username TEXT, created_by TEXT NOT NULL, created_at BIGINT NOT NULL, expires BIGINT NOT NULL,
  used_at BIGINT, used_by TEXT);
CREATE TABLE IF NOT EXISTS shares(
  token TEXT PRIMARY KEY, team_id TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, username TEXT, action TEXT NOT NULL, detail TEXT, at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS activity_team ON activity(team_id, at)
"""

DEFAULT_SETTINGS = {"teamName": "My team", "matchMin": 12, "subEvery": 3, "gkEvery": 6, "benchAlert": 6, "at": 0}


def now():
    return int(time.time())


def nid():
    return secrets.token_hex(8)


def norm(s):
    return str(s or "").strip().lower()


def clip(s, n=80):
    return str(s if s is not None else "")[:n]


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def log(c, team_id, username, action, detail=""):
    c.execute("INSERT INTO activity VALUES(?,?,?,?,?,?)", (nid(), team_id, username, action, clip(detail, 300), now()))


# ---------- teams & memberships ----------
def create_team(c, name, owner, settings=None):
    tid = nid()
    st = dict(DEFAULT_SETTINGS)
    st.update(settings or {})
    st["teamName"] = clip(name or st.get("teamName") or "My team", 60)
    c.execute("INSERT INTO teams VALUES(?,?,?,?,?)", (tid, st["teamName"], json.dumps(st), 1, now()))
    c.execute("INSERT INTO memberships VALUES(?,?,?,?,?)", (tid, owner, "admin", None, now()))
    log(c, tid, owner, "team", f"created team {st['teamName']}")
    return tid


def memberships(c, username):
    rows = c.execute("SELECT m.team_id, m.role, m.player_id, t.name FROM memberships m JOIN teams t ON t.id=m.team_id "
                     "WHERE m.username=? ORDER BY t.created_at", (username,)).fetchall()
    return [{"id": r["team_id"], "name": r["name"], "role": r["role"], "playerId": r["player_id"]} for r in rows]


def admin_count(c, team_id):
    return c.execute("SELECT COUNT(*) AS n FROM memberships WHERE team_id=? AND role='admin'", (team_id,)).fetchone()["n"]


# ---------- legacy migration (one JSON blob per user -> teams and tables) ----------
def team_from_blob(c, username, S, share_token=None):
    S = S or {}
    st = S.get("settings") or {}
    tid = create_team(c, st.get("teamName") or "My team", username, st)
    for p in S.get("squad") or []:
        if p.get("id"):
            c.execute("INSERT INTO players VALUES(?,?,?,?,?,0,?) ON CONFLICT(id) DO NOTHING",
                      (clip(p["id"], 64), tid, clip(p.get("name"), 60), clip(p.get("num"), 4),
                       1 if p.get("noGk") else 0, as_int(p.get("u"))))
    for t in S.get("teams") or []:
        if norm(t):
            c.execute("INSERT INTO opponents VALUES(?,?,?,0) ON CONFLICT(team_id, norm) DO NOTHING", (tid, norm(t), clip(t, 60)))
    for h in S.get("history") or []:
        insert_game(c, tid, h, username)
    m = dict(S.get("match") or {})
    if m.get("id"):
        events = m.pop("events", []) or []
        removed = set(m.pop("removed", []) or [])
        c.execute("INSERT INTO live VALUES(?,?,?)", (tid, json.dumps(m), now()))
        for e in events:
            insert_event(c, tid, m["id"], e, username, removed=e.get("id") in removed)
    if share_token:
        c.execute("INSERT INTO shares VALUES(?,?,?) ON CONFLICT(token) DO NOTHING", (share_token, tid, now()))
    return tid


def insert_game(c, tid, h, username):
    gid = clip(h.get("id") or h.get("date"), 64)
    if not gid or not h.get("date"):
        return False
    if c.execute("SELECT 1 FROM games WHERE id=?", (gid,)).fetchone():
        return False
    c.execute("INSERT INTO games VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
              (gid, tid, clip(h["date"], 40), clip(h.get("opp") or "Opponent", 60), as_int(h.get("us")), as_int(h.get("them")),
               as_int(h.get("len")), json.dumps(h.get("players") or {}), json.dumps(h.get("timeline") or []),
               clip(h.get("text"), 4000), username, now()))
    return True


def insert_event(c, tid, match_id, e, username, removed=False):
    if not e.get("id") or e.get("type") not in EVENT_TYPES:
        return False
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
              (clip(e["id"], 64), tid, clip(match_id, 64), as_int(e.get("t")), as_int(e.get("n")), e["type"],
               clip(e.get("pid"), 64) if e.get("pid") else None, clip(e.get("g"), 64), username, now(),
               1 if removed else 0, username if removed else None))
    return True


# ---------- reading: build the phone's state for one team, as one role sees it ----------
def assemble(c, tid, role="admin", player_id=None):
    t = c.execute("SELECT name, settings, version FROM teams WHERE id=?", (tid,)).fetchone()
    if not t:
        return None, 0
    settings = dict(DEFAULT_SETTINGS)
    settings.update(json.loads(t["settings"] or "{}"))
    settings["teamName"] = t["name"]
    prow = c.execute("SELECT id, name, num, no_gk, deleted, updated_at FROM players WHERE team_id=?", (tid,)).fetchall()
    orow = c.execute("SELECT norm, name, deleted FROM opponents WHERE team_id=?", (tid,)).fetchall()
    grow = c.execute("SELECT * FROM games WHERE team_id=? ORDER BY played_at DESC", (tid,)).fetchall()
    S = {
        "settings": settings,
        "squad": [{"id": p["id"], "name": p["name"], "num": p["num"] or "", "noGk": bool(p["no_gk"]), "u": p["updated_at"]}
                  for p in prow if not p["deleted"]],
        "removedPlayers": [p["id"] for p in prow if p["deleted"]],
        "teams": [o["name"] for o in orow if not o["deleted"]],
        "removedTeams": [o["norm"] for o in orow if o["deleted"]],
        "history": [{"id": g["id"], "date": g["played_at"], "opp": g["opponent"], "us": g["goals_for"], "them": g["goals_against"],
                     "len": g["len_ms"], "players": json.loads(g["players"] or "{}"), "timeline": json.loads(g["timeline"] or "[]"),
                     "text": g["summary"] or "", "savedBy": g["saved_by"]} for g in grow if not g["deleted"]],
        "removedMatches": [g["id"] for g in grow if g["deleted"]],
        "match": None,
    }
    lv = c.execute("SELECT match FROM live WHERE team_id=?", (tid,)).fetchone()
    if lv:
        m = json.loads(lv["match"])
        evs = c.execute("SELECT id, t, n, type, player_id, grp, created_by, removed FROM events WHERE team_id=? AND match_id=?",
                        (tid, m.get("id"))).fetchall()
        m["events"] = [{"id": e["id"], "t": e["t"], "n": e["n"], "type": e["type"], "pid": e["player_id"], "g": e["grp"],
                        "by": e["created_by"]} for e in evs]
        m["removed"] = [e["id"] for e in evs if e["removed"]]
        S["match"] = m
    if role == "parent":
        S = only_own_child(S, player_id)
    return S, t["version"]


def only_own_child(S, pid):
    """Parents see team results plus their own child's stats, never other children's."""
    S["squad"] = [p for p in S["squad"] if p["id"] == pid]
    S["removedPlayers"] = []
    for h in S["history"]:
        h["players"] = {k: v for k, v in (h.get("players") or {}).items() if k == pid}
        h["timeline"] = [dict(e, pid=e.get("pid") if e.get("pid") == pid else None,
                              gk=e.get("gk") if e.get("gk") == pid else None)
                         for e in (h.get("timeline") or []) if e.get("type") != "assist" or e.get("pid") == pid]
        h["text"] = f"{S['settings'].get('teamName', 'Us')} {h.get('us')}-{h.get('them')} {h.get('opp')}"
        h.pop("savedBy", None)
    m = S.get("match")
    if m:
        keep = []
        for e in m["events"]:
            e.pop("by", None)
            if e["type"] in ("kickoff", "pause", "resume"):
                keep.append(e)
            elif e["type"] in ("goal", "against"):
                keep.append(dict(e, pid=e["pid"] if e["pid"] == pid else None))
            elif e.get("pid") == pid:
                keep.append(e)
        m["events"] = keep
        ids = {e["id"] for e in keep}
        m["removed"] = [i for i in m.get("removed", []) if i in ids]
        m["plan"] = None
        m["absent"] = [a for a in m.get("absent", []) if a == pid]
    return S


# ---------- writing: apply a phone's changes, keeping only what this role may change ----------
def apply(c, tid, user, role, inc):
    """Returns True if some changes were not allowed (the phone should then reload the server's copy)."""
    admin = role == "admin"
    dropped = False
    cur, _ = assemble(c, tid, "admin")
    names = {p["id"]: p["name"] for p in cur["squad"]}

    # settings and team name
    ist = {k: v for k, v in (inc.get("settings") or {}).items() if k != "at"}
    cst = {k: v for k, v in cur["settings"].items() if k != "at"}
    if ist and ist != cst:
        if admin:
            st = dict(cur["settings"])
            for k in ("teamName", "matchMin", "subEvery", "gkEvery", "benchAlert", "at"):
                if k in (inc.get("settings") or {}):
                    st[k] = inc["settings"][k]
            st["teamName"] = clip(st.get("teamName") or "My team", 60)
            for k in ("matchMin", "subEvery", "gkEvery", "benchAlert"):
                st[k] = max(1, min(90, as_int(st.get(k), DEFAULT_SETTINGS[k])))
            c.execute("UPDATE teams SET name=?, settings=? WHERE id=?", (st["teamName"], json.dumps(st), tid))
            if st["teamName"] != cur["settings"]["teamName"]:
                log(c, tid, user, "settings", f"renamed team to {st['teamName']}")
        else:
            dropped = True

    # squad
    rows = {r["id"]: r for r in c.execute("SELECT id, name, num, no_gk, deleted, updated_at FROM players WHERE team_id=?", (tid,)).fetchall()}
    for p in inc.get("squad") or []:
        pid = clip(p.get("id"), 64)
        if not pid:
            continue
        r = rows.get(pid)
        new = (clip(p.get("name"), 60), clip(p.get("num"), 4), 1 if p.get("noGk") else 0)
        if r is None:
            if admin:
                c.execute("INSERT INTO players VALUES(?,?,?,?,?,0,?) ON CONFLICT(id) DO NOTHING", (pid, tid, *new, as_int(p.get("u"), now())))
                log(c, tid, user, "squad", f"added player {new[0]}")
            else:
                dropped = True
        elif r["deleted"]:
            dropped = True
        elif new != (r["name"], r["num"] or "", r["no_gk"]):
            if admin and as_int(p.get("u")) >= as_int(r["updated_at"]):
                c.execute("UPDATE players SET name=?, num=?, no_gk=?, updated_at=? WHERE id=?", (*new, as_int(p.get("u"), now()), pid))
                if new[0] != r["name"]:
                    log(c, tid, user, "squad", f"renamed {r['name']} to {new[0]}")
            elif not admin:
                dropped = True
    for pid in inc.get("removedPlayers") or []:
        r = rows.get(pid)
        if r and not r["deleted"]:
            if admin:
                c.execute("UPDATE players SET deleted=1, updated_at=? WHERE id=?", (now(), pid))
                log(c, tid, user, "squad", f"removed player {r['name']}")
            else:
                dropped = True

    # opponents (trackers may add new ones when saving a match, only admins remove)
    orows = {r["norm"]: r for r in c.execute("SELECT norm, name, deleted FROM opponents WHERE team_id=?", (tid,)).fetchall()}
    removed_teams = {norm(x) for x in inc.get("removedTeams") or []}
    for name in inc.get("teams") or []:
        n = norm(name)
        if not n or n in removed_teams:
            continue
        r = orows.get(n)
        if r is None:
            c.execute("INSERT INTO opponents VALUES(?,?,?,0)", (tid, n, clip(name, 60)))
        elif r["deleted"]:
            c.execute("UPDATE opponents SET deleted=0 WHERE team_id=? AND norm=?", (tid, n))
    for n in removed_teams:
        r = orows.get(n)
        if r and not r["deleted"]:
            if admin:
                c.execute("UPDATE opponents SET deleted=1 WHERE team_id=? AND norm=?", (tid, n))
                log(c, tid, user, "opponents", f"removed {r['name']}")
            else:
                dropped = True

    # saved matches: anyone who tracks can add one; only admins edit or delete
    grows = {r["id"]: r for r in c.execute("SELECT id, opponent, goals_for, goals_against, deleted FROM games WHERE team_id=?", (tid,)).fetchall()}
    for h in inc.get("history") or []:
        gid = clip(h.get("id") or h.get("date"), 64)
        r = grows.get(gid)
        if r is None:
            if insert_game(c, tid, h, user):
                log(c, tid, user, "match", f"saved {cur['settings']['teamName']} {as_int(h.get('us'))}-{as_int(h.get('them'))} {clip(h.get('opp'), 60)}")
        elif r["deleted"]:
            dropped = True
        else:
            new = (clip(h.get("opp") or "Opponent", 60), as_int(h.get("us")), as_int(h.get("them")))
            if new != (r["opponent"], r["goals_for"], r["goals_against"]):
                if admin:
                    c.execute("UPDATE games SET opponent=?, goals_for=?, goals_against=?, summary=? WHERE id=?",
                              (*new, clip(h.get("text"), 4000), gid))
                    log(c, tid, user, "match", f"edited match: now {new[1]}-{new[2]} vs {new[0]} (was {r['goals_for']}-{r['goals_against']} vs {r['opponent']})")
                else:
                    dropped = True
    for gid in inc.get("removedMatches") or []:
        r = grows.get(gid)
        if r and not r["deleted"]:
            if admin:
                c.execute("UPDATE games SET deleted=1 WHERE id=?", (gid,))
                log(c, tid, user, "match", f"deleted match {r['goals_for']}-{r['goals_against']} vs {r['opponent']}")
            else:
                dropped = True

    # live match
    im = inc.get("match")
    if isinstance(im, dict) and im.get("id"):
        dropped = apply_live(c, tid, user, admin, cur, im, names) or dropped
    return dropped


LIVE_KEYS = ("id", "day", "opp", "oppAt", "clock", "plan", "planAt", "absent", "absentAt")


def apply_live(c, tid, user, admin, cur, im, names):
    dropped = False
    cm = cur.get("match")
    # read saved matches fresh: a match saved in this same request counts as saved, not reset
    saved = {r["id"] for r in c.execute("SELECT id FROM games WHERE team_id=?", (tid,)).fetchall()}
    im_events = im.get("events") or []
    if cm and im["id"] == cm["id"]:
        have = {e["id"] for e in cm["events"]}
        for e in im_events:
            if e.get("id") not in have:
                insert_event(c, tid, cm["id"], e, user)
        undo = set(im.get("removed") or []) - set(cm.get("removed") or [])
        for eid in undo:
            c.execute("UPDATE events SET removed=1, removed_by=? WHERE id=? AND team_id=? AND match_id=? AND removed=0",
                      (user, clip(eid, 64), tid, cm["id"]))
        m = {k: cm.get(k) for k in LIVE_KEYS}
        if as_int((im.get("clock") or {}).get("at")) > as_int((cm.get("clock") or {}).get("at")):
            m["clock"] = sanitize_clock(im.get("clock"))
        if as_int(im.get("oppAt")) > as_int(cm.get("oppAt")):
            m["opp"], m["oppAt"] = clip(im.get("opp"), 60), as_int(im.get("oppAt"))
        for k in ("plan", "absent"):
            if as_int(im.get(k + "At")) > as_int(cm.get(k + "At")):
                if admin:
                    m[k], m[k + "At"] = im.get(k), as_int(im.get(k + "At"))
                else:
                    dropped = True
        c.execute("UPDATE live SET match=?, updated_at=? WHERE team_id=?", (json.dumps(m), now(), tid))
        return dropped
    if im["id"] in saved:
        return True  # an old phone still holds a match that is already saved
    m = {k: im.get(k) for k in LIVE_KEYS}
    m["id"], m["opp"], m["clock"] = clip(im["id"], 64), clip(im.get("opp"), 60), sanitize_clock(im.get("clock"))
    if not admin:
        same_day = cm and cm.get("day") == im.get("day")
        m["plan"], m["planAt"] = None, 0
        m["absent"], m["absentAt"] = (cm.get("absent") or [], cm.get("absentAt") or 0) if same_day else ([], 0)
    if cm and cm["id"] not in saved and any(e["type"] in ("goal", "against", "on") for e in cm["events"]):
        log(c, tid, user, "match", f"reset an unsaved match vs {cm.get('opp') or 'opponent'}")
    c.execute("INSERT INTO live VALUES(?,?,?) ON CONFLICT(team_id) DO UPDATE SET match=excluded.match, updated_at=excluded.updated_at",
              (tid, json.dumps(m), now()))
    removed = set(im.get("removed") or [])
    for e in im_events:
        insert_event(c, tid, m["id"], e, user, removed=e.get("id") in removed)
    return dropped


def sanitize_clock(cl):
    cl = cl if isinstance(cl, dict) else {}
    return {"running": bool(cl.get("running")), "startedAt": as_int(cl.get("startedAt")),
            "elapsed": as_int(cl.get("elapsed")), "at": as_int(cl.get("at"))}


# ---------- activity log ----------
EVENT_WORDS = {"goal": "goal", "assist": "assist", "save": "save/tackle", "shot": "shot", "on": "sub on", "off": "sub off",
               "against": "goal against", "gk": "in goal", "kickoff": "kick off", "pause": "paused clock", "resume": "restarted clock"}


def activity(c, tid, limit=150):
    names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM players WHERE team_id=?", (tid,)).fetchall()}
    items = [{"at": r["at"], "who": r["username"], "kind": r["action"], "text": r["detail"]}
             for r in c.execute("SELECT username, action, detail, at FROM activity WHERE team_id=? ORDER BY at DESC LIMIT ?", (tid, limit)).fetchall()]
    for e in c.execute("SELECT type, player_id, t, created_by, created_at, removed, removed_by FROM events WHERE team_id=? "
                       "ORDER BY created_at DESC LIMIT ?", (tid, limit)).fetchall():
        what = EVENT_WORDS.get(e["type"], e["type"])
        who_for = f" · {names.get(e['player_id'], 'unknown player')}" if e["player_id"] else ""
        mins = f"{as_int(e['t']) // 60000 + 1}'"
        text = f"{mins} {what}{who_for}"
        if e["removed"]:
            text += f" (undone by {e['removed_by']})"
        items.append({"at": e["created_at"], "who": e["created_by"], "kind": "event", "text": text})
    items.sort(key=lambda x: x["at"], reverse=True)
    return items[:limit]
