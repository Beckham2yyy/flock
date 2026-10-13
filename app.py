#!/usr/bin/env python3
"""
Flock — reciprocal member exchange (no auto-follow API).

PIN login (OAuth 1.0a) → live pool of linked members → pair matches →
user follows on X via intent link → we verify → credit only on mutual FB.
Free-riders lose pool priority and can be locked out of Start.
"""

from __future__ import annotations

import os
import sqlite3
import time
from functools import wraps

import requests
from flask import Flask, g, jsonify, redirect, render_template_string, request, session, url_for
from requests_oauthlib import OAuth1, OAuth1Session

SECRET_KEY = os.environ.get("FLOCK_SECRET", "dev-only-change-me")
TWITTER_API_KEY = "KBBYa09PpqRMJFQ2c7iP6vIOu"
TWITTER_API_SECRET = "fncXCYchvOVRGx2nYu1NIPktIx8D1D8r538Efo9BqdiVBOzahQ"

MAX_CREDITS = 30
HOUR_SECONDS = int(os.environ.get("FLOCK_HOUR", "3600"))
PIN_TTL = 7200
MATCH_TTL = 48 * 3600          # pending pair expires after 48h
MIN_RECIP_RATIO = 0.5          # below this → deprioritized / may be locked
MIN_ACTIONS_FOR_LOCK = 5       # need some history before lock applies
START_BATCH = 5                # pairs opened per Start click (capped by credits)

REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
VERIFY_URL = "https://api.twitter.com/1.1/account/verify_credentials.json"
FRIENDSHIP_URL = "https://api.twitter.com/1.1/friendships/show.json"

_IS_VERCEL = bool(os.environ.get("VERCEL"))
DB_PATH = os.environ.get(
    "FLOCK_DB",
    "/tmp/flock.db"
    if _IS_VERCEL
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "flock.db"),
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = 60 * 60 * 24 * 30


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        try:
            g.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            handle TEXT PRIMARY KEY,
            name TEXT,
            bio TEXT,
            pin TEXT,
            pin_at REAL,
            req_token TEXT,
            req_secret TEXT,
            access_token TEXT,
            access_secret TEXT,
            credits INTEGER NOT NULL DEFAULT 30,
            reset_at REAL,
            given INTEGER NOT NULL DEFAULT 0,
            received INTEGER NOT NULL DEFAULT 0,
            expired INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            a_handle TEXT NOT NULL,
            b_handle TEXT NOT NULL,
            a_done INTEGER NOT NULL DEFAULT 0,
            b_done INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            completed_at REAL,
            UNIQUE(a_handle, b_handle, status)
        );
        CREATE INDEX IF NOT EXISTS pairs_a ON pairs(a_handle, status);
        CREATE INDEX IF NOT EXISTS pairs_b ON pairs(b_handle, status);
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, typ, default in [
        ("req_token", "TEXT", None),
        ("req_secret", "TEXT", None),
        ("access_token", "TEXT", None),
        ("access_secret", "TEXT", None),
        ("given", "INTEGER NOT NULL DEFAULT 0", 0),
        ("received", "INTEGER NOT NULL DEFAULT 0", 0),
        ("expired", "INTEGER NOT NULL DEFAULT 0", 0),
        ("credits", "INTEGER NOT NULL DEFAULT 30", MAX_CREDITS),
        ("reset_at", "REAL", None),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


init_db()


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if not session.get("handle"):
            return redirect(url_for("home"))
        return fn(*args, **kwargs)

    return wrap


def sanitize(raw: str) -> str:
    return "".join(c for c in (raw or "").lstrip("@") if c.isalnum() or c == "_")[:15]


def fmt(seconds: float) -> str:
    s = max(0, int(seconds + 0.999))
    return f"{s // 60:02d}:{s % 60:02d}"


def get_user(handle: str):
    return db().execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()


def upsert_user(handle: str):
    now = time.time()
    if get_user(handle) is None:
        db().execute(
            """
            INSERT INTO users (handle, name, bio, credits, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (handle, handle, "Flock member", MAX_CREDITS, now, now),
        )
        db().commit()
    else:
        db().execute("UPDATE users SET last_seen = ? WHERE handle = ?", (now, handle))
        db().commit()


def oauth1_for(u) -> OAuth1 | None:
    if not u or not u["access_token"] or not u["access_secret"]:
        return None
    return OAuth1(
        TWITTER_API_KEY,
        TWITTER_API_SECRET,
        u["access_token"],
        u["access_secret"],
    )


def recip_ratio(u) -> float:
    given = int(u["given"] or 0)
    received = int(u["received"] or 0)
    expired = int(u["expired"] or 0)
    denom = given + expired
    if denom <= 0:
        return 1.0  # new users start neutral-good
    # received follow-backs relative to outbound attempts
    return received / max(denom, 1)


def is_locked(u) -> bool:
    """Chronic free-riders can't open new Start batches."""
    given = int(u["given"] or 0)
    received = int(u["received"] or 0)
    expired = int(u["expired"] or 0)
    total = given + expired
    if total < MIN_ACTIONS_FOR_LOCK:
        return False
    return recip_ratio(u) < MIN_RECIP_RATIO and received < given * MIN_RECIP_RATIO


def apply_reset(u):
    now = time.time()
    if u["reset_at"] and now >= u["reset_at"]:
        db().execute(
            "UPDATE users SET credits = ?, reset_at = NULL WHERE handle = ?",
            (MAX_CREDITS, u["handle"]),
        )
        db().commit()
        return get_user(u["handle"])
    return u


def expire_stale_pairs():
    now = time.time()
    cutoff = now - MATCH_TTL
    rows = db().execute(
        "SELECT * FROM pairs WHERE status = 'pending' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    for p in rows:
        db().execute(
            "UPDATE pairs SET status = 'expired' WHERE id = ?",
            (p["id"],),
        )
        # punish both sides slightly on expiry of open pairs
        for h in (p["a_handle"], p["b_handle"]):
            db().execute(
                "UPDATE users SET expired = expired + 1 WHERE handle = ?",
                (h,),
            )
    if rows:
        db().commit()


def linked_users(exclude: str | None = None):
    q = """
        SELECT * FROM users
        WHERE access_token IS NOT NULL AND access_token != ''
          AND access_secret IS NOT NULL AND access_secret != ''
    """
    args: list = []
    if exclude:
        q += " AND handle != ?"
        args.append(exclude)
    rows = db().execute(q, args).fetchall()
    # sort by reciprocity desc, then last_seen
    scored = []
    for r in rows:
        if is_locked(r) and r["handle"] != exclude:
            continue
        scored.append((recip_ratio(r), r["last_seen"] or 0, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored]


def stats():
    now = time.time()
    total = db().execute(
        """
        SELECT COUNT(*) c FROM users
        WHERE access_token IS NOT NULL AND access_token != ''
        """
    ).fetchone()["c"]
    active = db().execute(
        """
        SELECT COUNT(*) c FROM users
        WHERE access_token IS NOT NULL AND access_token != ''
          AND last_seen > ?
        """,
        (now - 86400,),
    ).fetchone()["c"]
    day = db().execute(
        """
        SELECT COUNT(*) c FROM users
        WHERE access_token IS NOT NULL AND access_token != ''
          AND created_at > ?
        """,
        (now - 86400,),
    ).fetchone()["c"]
    return {"total": total, "active": active, "day": day}


def open_pair_exists(a: str, b: str) -> bool:
    row = db().execute(
        """
        SELECT id FROM pairs
        WHERE status = 'pending'
          AND ((a_handle = ? AND b_handle = ?) OR (a_handle = ? AND b_handle = ?))
        """,
        (a, b, b, a),
    ).fetchone()
    return row is not None


def create_pairs_for(handle: str, n: int) -> int:
    """Open up to n new pairs for handle with other linked members."""
    u = get_user(handle)
    if not u or not u["access_token"] or is_locked(u):
        return 0
    pool = linked_users(exclude=handle)
    created = 0
    now = time.time()
    for other in pool:
        if created >= n:
            break
        if open_pair_exists(handle, other["handle"]):
            continue
        # canonical order: lexicographic so UNIQUE works cleanly
        a, b = sorted([handle, other["handle"]])
        try:
            db().execute(
                """
                INSERT INTO pairs (a_handle, b_handle, a_done, b_done, status, created_at)
                VALUES (?, ?, 0, 0, 'pending', ?)
                """,
                (a, b, now),
            )
            created += 1
        except sqlite3.IntegrityError:
            continue
    db().commit()
    return created


def pairs_for(handle: str):
    return db().execute(
        """
        SELECT * FROM pairs
        WHERE status = 'pending' AND (a_handle = ? OR b_handle = ?)
        ORDER BY created_at DESC
        """,
        (handle, handle),
    ).fetchall()


def completed_for(handle: str, limit: int = 30):
    return db().execute(
        """
        SELECT * FROM pairs
        WHERE status = 'completed' AND (a_handle = ? OR b_handle = ?)
        ORDER BY completed_at DESC LIMIT ?
        """,
        (handle, handle, limit),
    ).fetchall()


def check_following(source_user, target_screen: str) -> tuple[bool, str]:
    """
    Ask X if source follows target (read API).
    Returns (is_following, detail).
    """
    auth = oauth1_for(source_user)
    if not auth:
        return False, "not linked"
    try:
        r = requests.get(
            FRIENDSHIP_URL,
            params={
                "source_screen_name": source_user["handle"],
                "target_screen_name": target_screen,
            },
            auth=auth,
            timeout=20,
        )
        if r.status_code != 200:
            return False, f"{r.status_code}: {r.text[:120]}"
        data = r.json()
        rel = data.get("relationship") or {}
        source = rel.get("source") or {}
        following = bool(source.get("following"))
        return following, "ok" if following else "not following yet"
    except Exception as e:
        return False, str(e)[:120]


def mark_side_done(pair_id: int, handle: str) -> str:
    p = db().execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if not p or p["status"] != "pending":
        return "not found"
    if handle == p["a_handle"]:
        db().execute("UPDATE pairs SET a_done = 1 WHERE id = ?", (pair_id,))
        side = "a"
    elif handle == p["b_handle"]:
        db().execute("UPDATE pairs SET b_done = 1 WHERE id = ?", (pair_id,))
        side = "b"
    else:
        return "forbidden"
    db().commit()
    p = db().execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if p["a_done"] and p["b_done"]:
        complete_pair(p)
        return "mutual"
    return f"{side}_done"


def complete_pair(p):
    now = time.time()
    db().execute(
        "UPDATE pairs SET status = 'completed', completed_at = ? WHERE id = ?",
        (now, p["id"]),
    )
    # each side: +1 given (they followed), +1 received (got FB)
    for h in (p["a_handle"], p["b_handle"]):
        db().execute(
            """
            UPDATE users
            SET given = given + 1, received = received + 1, last_seen = ?
            WHERE handle = ?
            """,
            (now, h),
        )
    db().commit()


def verify_pair_for_user(pair_id: int, handle: str) -> dict:
    p = db().execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if not p or p["status"] != "pending":
        return {"ok": False, "error": "Pair not open"}
    if handle not in (p["a_handle"], p["b_handle"]):
        return {"ok": False, "error": "Not your pair"}

    u = get_user(handle)
    partner = p["b_handle"] if handle == p["a_handle"] else p["a_handle"]
    following, detail = check_following(u, partner)
    if not following:
        return {
            "ok": False,
            "error": detail if detail != "ok" else f"X does not show you following @{partner} yet. Use the Follow button, then Verify again.",
            "partner": partner,
        }

    result = mark_side_done(pair_id, handle)
    msg = (
        f"Mutual complete with @{partner} — both got credit toward reputation."
        if result == "mutual"
        else f"Your follow of @{partner} recorded. Waiting for them to follow you back."
    )
    return {"ok": True, "result": result, "message": msg, "partner": partner}


def user_state(u):
    expire_stale_pairs()
    u = apply_reset(get_user(u["handle"]))
    now = time.time()
    remain = max(0.0, (u["reset_at"] or 0) - now) if u["reset_at"] else 0.0
    pending = pairs_for(u["handle"])
    done = completed_for(u["handle"])
    ratio = recip_ratio(u)
    locked = is_locked(u)
    pool_n = len(linked_users(exclude=u["handle"]))

    tasks = []
    for p in pending:
        partner = p["b_handle"] if p["a_handle"] == u["handle"] else p["a_handle"]
        my_done = p["a_done"] if p["a_handle"] == u["handle"] else p["b_done"]
        their_done = p["b_done"] if p["a_handle"] == u["handle"] else p["a_done"]
        left = max(0, MATCH_TTL - (now - p["created_at"]))
        tasks.append(
            {
                "id": p["id"],
                "partner": partner,
                "my_done": bool(my_done),
                "their_done": bool(their_done),
                "follow_url": f"https://x.com/intent/follow?screen_name={partner}",
                "expires_in": left,
                "expires_label": fmt(left),
            }
        )

    history = []
    for p in done:
        partner = p["b_handle"] if p["a_handle"] == u["handle"] else p["a_handle"]
        history.append({"partner": partner, "at": p["completed_at"]})

    if locked:
        msg = (
            f"Locked from new matches — reciprocity {ratio:.0%} "
            f"(need ≥ {MIN_RECIP_RATIO:.0%}). Complete pending FBs or wait for better history."
        )
    elif pool_n == 0:
        msg = "You are the only linked member. Invite others to authorize — pairs need 2+ accounts."
    elif not tasks:
        msg = "Press Start to open pair matches. Follow on X, then Verify. Credit only when they FB."
    else:
        msg = f"{len(tasks)} open pair(s). Follow → Verify. Mutual FB required."

    return {
        "handle": u["handle"],
        "credits": u["credits"],
        "remain": remain,
        "remain_label": fmt(remain) if remain else ("Ready" if u["credits"] > 0 else "00:00"),
        "ratio": ratio,
        "ratio_label": f"{ratio:.0%}",
        "given": int(u["given"] or 0),
        "received": int(u["received"] or 0),
        "expired": int(u["expired"] or 0),
        "locked": locked,
        "linked": bool(u["access_token"]),
        "pool": pool_n,
        "tasks": tasks,
        "history": history,
        "message": msg,
        "hour": HOUR_SECONDS,
        "stats": stats(),
    }


PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Flock</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg:#0b1017; --surface:#141c26; --elevated:#1b2531; --fg:#f3ece1;
    --muted:#8b97a6; --subtle:#6b7684; --primary:#e39a32; --pfg:#1a1208; --ok:#7dba7a; --bad:#d36b5a;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:Outfit,system-ui,sans-serif; }
  h1,h2,h3 { font-family:Fraunces,Georgia,serif; font-weight:500; }
  a { color:inherit; text-decoration:none; }
  a.link { color:var(--primary); text-decoration:underline; }
  .wrap { max-width:960px; margin:0 auto; padding:0 16px 80px; }
  header { position:sticky; top:0; z-index:10; backdrop-filter:blur(10px); background:rgba(11,16,23,.85); border-bottom:1px solid rgba(243,236,225,.08); }
  header .bar { max-width:960px; margin:0 auto; height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 16px; }
  .brand { display:flex; gap:8px; align-items:center; font-family:Fraunces,serif; font-size:18px; }
  .logo { width:28px; height:28px; border-radius:6px; background:var(--elevated); display:grid; place-items:center; }
  .kicker { font-size:11px; letter-spacing:.18em; text-transform:uppercase; color:var(--primary); font-weight:500; }
  .muted { color:var(--muted); }
  .subtle { color:var(--subtle); font-size:12px; }
  .grid2 { display:grid; gap:20px; margin-top:40px; }
  @media (min-width:900px) { .grid2 { grid-template-columns:1fr 1fr; } }
  .card { background:var(--surface); border-radius:16px; padding:24px; box-shadow:0 0 0 1px rgba(243,236,225,.08); }
  label { display:block; font-size:12px; color:var(--muted); margin-bottom:6px; }
  input { width:100%; height:44px; border:0; border-radius:10px; background:var(--elevated); color:var(--fg); padding:0 14px; font:inherit; box-shadow:0 0 0 1px rgba(243,236,225,.08); }
  input:focus { outline:2px solid var(--primary); }
  .handle { position:relative; }
  .handle span { position:absolute; left:14px; top:50%; transform:translateY(-50%); color:var(--subtle); }
  .handle input { padding-left:28px; }
  button, .btn {
    font:inherit; cursor:pointer; border:0; border-radius:10px; height:48px; padding:0 18px;
    background:var(--primary); color:var(--pfg); font-weight:500; display:inline-flex; align-items:center; justify-content:center;
  }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost, .btn.ghost { background:transparent; color:var(--muted); height:36px; box-shadow:0 0 0 1px rgba(243,236,225,.12); }
  button.full { width:100%; margin-top:14px; }
  .pin { font-family:Fraunces,serif; font-size:18px; margin:8px 0 0; word-break:break-all; }
  .meters { display:grid; gap:12px; margin:28px 0 12px; }
  @media (min-width:700px) { .meters { grid-template-columns:repeat(4,1fr); } }
  .meter { background:var(--surface); border-radius:16px; padding:16px; box-shadow:0 0 0 1px rgba(243,236,225,.08); }
  .meter b { display:block; font-family:Fraunces,serif; font-size:26px; font-weight:500; font-variant-numeric:tabular-nums; }
  .bar { height:6px; background:var(--surface); border-radius:99px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--primary); width:0%; }
  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:20px 0; }
  .task { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; padding:14px 16px; border-top:1px solid rgba(243,236,225,.08); }
  .ok { color:var(--ok); font-size:12px; }
  .bad { color:var(--bad); font-size:12px; }
  .how { display:grid; gap:12px; margin-top:24px; }
  @media (min-width:700px) { .how { grid-template-columns:repeat(3,1fr); } }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td,th { text-align:left; padding:10px 12px; border-top:1px solid rgba(243,236,225,.08); }
  .err { color:var(--bad); font-size:14px; margin-top:10px; }
  .msg { color:var(--muted); font-size:14px; margin-top:10px; }
  .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:24px; }
  .log { background:var(--surface); border-radius:16px; box-shadow:0 0 0 1px rgba(243,236,225,.08); overflow:hidden; margin-top:20px; }
  .log h2 { font-size:14px; margin:0; padding:12px 16px; border-bottom:1px solid rgba(243,236,225,.08); font-family:Outfit,sans-serif; }
</style>
</head>
<body>
<header>
  <div class="bar">
    <a class="brand" href="/"><span class="logo">✦</span> Flock</a>
    {% if handle %}
      <div>
        <span class="muted">@{{ handle }}</span>
        <form method="post" action="{{ url_for('logout') }}" style="display:inline">
          <button class="ghost" type="submit">Log out</button>
        </form>
      </div>
    {% endif %}
  </div>
</header>

{% if not handle %}
<main class="wrap" style="padding-top:40px">
  <p class="kicker">Reciprocal exchange</p>
  <h1>Follow real members. Get followed back.</h1>
  <p class="muted" style="max-width:560px">
    Authorize with X PIN. Start opens <strong>pair matches</strong>.
    You follow on X; they must follow you back. Credit and reputation only on <strong>mutual</strong> FB.
    Free-riders get deprioritized and can be locked out of new matches.
  </p>
  <div class="stats">
    <div class="meter"><span class="subtle">LINKED</span><b>{{ st.total }}</b></div>
    <div class="meter"><span class="subtle">ACTIVE 24H</span><b>{{ st.active }}</b></div>
    <div class="meter"><span class="subtle">NEW 24H</span><b>{{ st.day }}</b></div>
  </div>
  <div class="grid2">
    <div class="card">
      <p class="subtle">STEP 1</p>
      <h2>Get PIN on X</h2>
      <form method="post" action="{{ url_for('mint') }}">
        <label for="uh">Your X username</label>
        <div class="handle"><span>@</span>
          <input id="uh" name="handle" value="{{ form_handle }}" placeholder="yourhandle" required/>
        </div>
        <button class="full" type="submit">Get Pin Code On Twitter</button>
      </form>
      {% if auth_url %}
        <div class="card" style="background:var(--elevated);margin-top:16px;padding:16px">
          <p class="subtle">Open, Authorize app, copy PIN:</p>
          <p class="pin"><a class="link" href="{{ auth_url }}" target="_blank" rel="noopener">{{ auth_url }}</a></p>
        </div>
      {% endif %}
    </div>
    <div class="card">
      <p class="subtle">STEP 2</p>
      <h2>Enter PIN</h2>
      <form method="post" action="{{ url_for('login') }}">
        <input type="hidden" name="handle" value="{{ form_handle }}"/>
        <label for="pin">PIN from X</label>
        <input id="pin" name="pin" inputmode="numeric" maxlength="7" placeholder="7 digits" required/>
        <button class="full" type="submit">Login</button>
      </form>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      {% if info %}<p class="msg">{{ info }}</p>{% endif %}
    </div>
  </div>
  <h2 style="margin-top:56px">Rules</h2>
  <div class="how">
    <div class="card"><p class="kicker">01</p><h3>Pairs only</h3><p class="muted">Start matches you with other linked members — not random bots.</p></div>
    <div class="card"><p class="kicker">02</p><h3>Mutual FB</h3><p class="muted">You follow on X; they must FB. No mutual → no reputation credit.</p></div>
    <div class="card"><p class="kicker">03</p><h3>No free rides</h3><p class="muted">Low reciprocity buries you in the pool and can lock Start.</p></div>
  </div>
  {{ member_table|safe }}
</main>
{% else %}
<main class="wrap" style="padding-top:32px">
  <p class="kicker">Dashboard</p>
  <h1>Pair exchange</h1>
  <p class="muted">Pool <strong id="pool">{{ state.pool }}</strong> · reciprocity <strong id="ratio">{{ state.ratio_label }}</strong></p>
  <div class="meters">
    <div class="meter"><span class="subtle">CREDITS</span><b id="credits">{{ state.credits }}</b><span class="subtle">new pairs / hour</span></div>
    <div class="meter"><span class="subtle">TIMER</span><b id="remain">{{ state.remain_label }}</b><span class="subtle">until refill</span></div>
    <div class="meter"><span class="subtle">GIVEN / GOT</span><b id="gr">{{ state.given }}/{{ state.received }}</b><span class="subtle">mutual completes</span></div>
    <div class="meter"><span class="subtle">EXPIRED</span><b id="exp">{{ state.expired }}</b><span class="subtle">missed FBs</span></div>
  </div>
  <div class="bar"><i id="bar"></i></div>
  <div class="row">
    <button id="start" type="button">Start</button>
    <p class="muted" id="hint"></p>
  </div>
  <p class="msg" id="msg">{{ state.message }}</p>
  <p class="err" id="err" style="display:none"></p>

  <div class="log">
    <h2>Open pairs — follow on X, then Verify</h2>
    <div id="tasks"></div>
  </div>
  <div class="log">
    <h2>Completed mutuals</h2>
    <div id="history"></div>
  </div>
  {{ member_table|safe }}
</main>
<script>
const HOUR = {{ hour }};
function esc(s){ return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function render(s){
  document.getElementById('credits').textContent = s.credits;
  document.getElementById('remain').textContent = s.remain_label;
  document.getElementById('gr').textContent = s.given + '/' + s.received;
  document.getElementById('exp').textContent = s.expired;
  document.getElementById('ratio').textContent = s.ratio_label;
  document.getElementById('pool').textContent = s.pool;
  document.getElementById('msg').textContent = s.message || '';
  document.getElementById('bar').style.width = (s.remain > 0 ? Math.min(100, s.remain / HOUR * 100) : 0) + '%';
  const can = s.linked && !s.locked && s.pool > 0 && s.credits > 0;
  document.getElementById('start').disabled = !can;
  document.getElementById('hint').textContent = s.locked
    ? 'Improve reciprocity (complete FBs) to unlock Start.'
    : (s.pool <= 0 ? 'Need other linked members.'
    : (s.credits <= 0 ? 'Wait for timer to refill pair credits.'
    : 'Opens up to {{ batch }} new pairs (1 credit each).'));

  const tasks = document.getElementById('tasks');
  if (!s.tasks.length) {
    tasks.innerHTML = '<div class="task muted">No open pairs. Press Start.</div>';
  } else {
    tasks.innerHTML = s.tasks.map(t => `
      <div class="task">
        <div>
          <strong>@${esc(t.partner)}</strong>
          <div class="subtle">
            You: ${t.my_done ? '✓ followed' : 'need follow'} ·
            Them: ${t.their_done ? '✓ FB' : 'waiting FB'} ·
            expires ${esc(t.expires_label)}
          </div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn ghost" href="${esc(t.follow_url)}" target="_blank" rel="noopener">Follow on X</a>
          <button type="button" data-id="${t.id}" class="verify" ${t.my_done ? 'disabled' : ''}>Verify</button>
        </div>
      </div>`).join('');
    tasks.querySelectorAll('.verify').forEach(btn => {
      btn.onclick = async () => {
        document.getElementById('err').style.display = 'none';
        const r = await fetch('/api/verify', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ pair_id: Number(btn.dataset.id) })
        });
        const j = await r.json();
        if (!j.ok) {
          document.getElementById('err').style.display = 'block';
          document.getElementById('err').textContent = j.error || 'Verify failed';
        }
        pull();
      };
    });
  }
  const hist = document.getElementById('history');
  hist.innerHTML = s.history.length
    ? s.history.map(h => `<div class="task"><span>@${esc(h.partner)}</span><span class="ok">Mutual</span></div>`).join('')
    : '<div class="task muted">No mutuals yet.</div>';
}
async function pull(){ render(await (await fetch('/api/state')).json()); }
document.getElementById('start').onclick = async () => {
  document.getElementById('err').style.display = 'none';
  const r = await fetch('/api/start', { method:'POST' });
  const j = await r.json();
  if (j.error) {
    document.getElementById('err').style.display = 'block';
    document.getElementById('err').textContent = j.error;
  }
  pull();
};
setInterval(pull, 5000);
pull();
</script>
{% endif %}
</body>
</html>
"""


def member_table_html(exclude: str | None = None) -> str:
    pool = linked_users(exclude=exclude)
    if not pool:
        body = "<tr><td colspan='3' class='muted'>No other linked members yet.</td></tr>"
    else:
        body = "".join(
            f"<tr><td>@{r['handle']}</td>"
            f"<td class='muted'>{(r['bio'] or '—')[:60]}</td>"
            f"<td class='ok'>{recip_ratio(r):.0%} recip</td></tr>"
            for r in pool[:40]
        )
    return (
        "<h2 style='margin-top:56px'>Members (by reciprocity)</h2>"
        "<div class='card' style='padding:0;overflow:auto'>"
        "<table><thead><tr><th>User</th><th>Bio</th><th>Score</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        "<p class='subtle' style='margin-top:24px;max-width:640px'>"
        "Pairs only. Mutual follow-back required. "
        f"Pending expires in {MATCH_TTL // 3600}h. "
        f"Start uses up to {START_BATCH} credits per click.</p>"
    )


@app.get("/")
def home():
    expire_stale_pairs()
    if session.get("handle"):
        u = get_user(session["handle"])
        if u:
            u = apply_reset(u)
            return render_template_string(
                PAGE,
                handle=u["handle"],
                state=user_state(u),
                max_credits=MAX_CREDITS,
                hour=HOUR_SECONDS,
                batch=START_BATCH,
                member_table=member_table_html(u["handle"]),
                form_handle="",
                auth_url=None,
                error=None,
                info=None,
                st=stats(),
            )
        session.clear()
    return render_template_string(
        PAGE,
        handle=None,
        form_handle=session.get("pending_handle", ""),
        auth_url=session.get("auth_url"),
        error=request.args.get("error"),
        info=request.args.get("info"),
        member_table=member_table_html(),
        max_credits=MAX_CREDITS,
        hour=HOUR_SECONDS,
        batch=START_BATCH,
        state=None,
        st=stats(),
    )


@app.post("/mint")
def mint():
    handle = sanitize(request.form.get("handle", ""))
    if len(handle) < 2:
        return redirect(url_for("home", error="Enter your X username."))

    oauth = OAuth1Session(
        TWITTER_API_KEY,
        client_secret=TWITTER_API_SECRET,
        callback_uri="oob",
    )
    try:
        tokens = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except Exception as e:
        return redirect(url_for("home", error=f"Could not start X authorize: {e}"))

    req_token = tokens.get("oauth_token")
    req_secret = tokens.get("oauth_token_secret")
    auth_url = f"{AUTHORIZE_URL}?oauth_token={req_token}"

    upsert_user(handle)
    db().execute(
        """
        UPDATE users
        SET req_token = ?, req_secret = ?, pin = NULL, pin_at = ?, last_seen = ?
        WHERE handle = ?
        """,
        (req_token, req_secret, time.time(), time.time(), handle),
    )
    db().commit()

    session["pending_handle"] = handle
    session["auth_url"] = auth_url
    return redirect(
        url_for("home", info="Open the link, Authorize app on X, then enter the PIN.")
    )


@app.post("/login")
def login():
    handle = sanitize(request.form.get("handle") or session.get("pending_handle", ""))
    pin = "".join(c for c in (request.form.get("pin") or "") if c.isdigit())[:7]
    if not handle or len(pin) < 6:
        return redirect(url_for("home", error="Enter the 7-digit PIN from X."))

    u = get_user(handle)
    if not u or not u["req_token"] or not u["req_secret"]:
        return redirect(url_for("home", error="Get a PIN link first (Step 1)."))

    if u["pin_at"] and time.time() - u["pin_at"] > PIN_TTL:
        return redirect(url_for("home", error="Authorize link expired. Start Step 1 again."))

    oauth = OAuth1Session(
        TWITTER_API_KEY,
        client_secret=TWITTER_API_SECRET,
        resource_owner_key=u["req_token"],
        resource_owner_secret=u["req_secret"],
        verifier=pin,
    )
    try:
        access = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception:
        return redirect(url_for("home", error="Invalid or expired PIN. Authorize again."))

    access_token = access.get("oauth_token")
    access_secret = access.get("oauth_token_secret")
    screen_name = sanitize(access.get("screen_name") or handle)

    auth = OAuth1(TWITTER_API_KEY, TWITTER_API_SECRET, access_token, access_secret)
    try:
        vr = requests.get(VERIFY_URL, auth=auth, params={"skip_status": "true"}, timeout=20)
        if vr.status_code == 200:
            data = vr.json()
            screen_name = sanitize(data.get("screen_name") or screen_name)
            name = data.get("name") or screen_name
            bio = (data.get("description") or "Flock member")[:120]
        else:
            name, bio = screen_name, "Flock member"
    except Exception:
        name, bio = screen_name, "Flock member"

    now = time.time()
    existing = get_user(screen_name)
    if existing is None and screen_name != handle:
        db().execute(
            """
            INSERT INTO users (
                handle, name, bio, access_token, access_secret,
                credits, created_at, last_seen, req_token, req_secret
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (screen_name, name, bio, access_token, access_secret, MAX_CREDITS, now, now),
        )
    else:
        target = screen_name if existing or screen_name == handle else handle
        if get_user(screen_name):
            target = screen_name
        db().execute(
            """
            UPDATE users
            SET name = ?, bio = ?, access_token = ?, access_secret = ?,
                req_token = NULL, req_secret = NULL, last_seen = ?
            WHERE handle = ?
            """,
            (name, bio, access_token, access_secret, now, target),
        )
        screen_name = target
    db().commit()

    session.clear()
    session["handle"] = screen_name
    session.permanent = True
    return redirect(url_for("home"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/api/state")
@login_required
def api_state():
    u = get_user(session["handle"])
    if not u:
        session.clear()
        return jsonify({"error": "session"}), 401
    return jsonify(user_state(u))


@app.post("/api/start")
@login_required
def api_start():
    expire_stale_pairs()
    u = apply_reset(get_user(session["handle"]))
    if not u:
        return jsonify({"error": "session"}), 401
    if not u["access_token"]:
        return jsonify({"error": "Link X first"}), 403
    if is_locked(u):
        return jsonify({"error": "Locked — improve reciprocity (complete follow-backs)"}), 403
    if u["credits"] <= 0:
        return jsonify({"error": "No pair credits left this hour"}), 409
    if not linked_users(exclude=u["handle"]):
        return jsonify({"error": "Need other linked members in the pool"}), 409

    n = min(START_BATCH, int(u["credits"]))
    created = create_pairs_for(u["handle"], n)
    if created <= 0:
        return jsonify({"error": "No new partners available (all paired or empty)"}), 409

    credits = int(u["credits"]) - created
    reset_at = u["reset_at"]
    if credits <= 0 and not reset_at:
        reset_at = time.time() + HOUR_SECONDS
    db().execute(
        "UPDATE users SET credits = ?, reset_at = ?, last_seen = ? WHERE handle = ?",
        (credits, reset_at, time.time(), u["handle"]),
    )
    db().commit()
    return jsonify(user_state(get_user(u["handle"])))


@app.post("/api/verify")
@login_required
def api_verify():
    data = request.get_json(silent=True) or {}
    try:
        pair_id = int(data.get("pair_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad pair id"}), 400
    handle = session["handle"]
    result = verify_pair_for_user(pair_id, handle)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3333"))
    print(f"Flock reciprocal  http://0.0.0.0:{port}  db={DB_PATH}")
    app.run(host="0.0.0.0", port=port, debug=False)
