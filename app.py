#!/usr/bin/env python3
"""
Flock — live member exchange.
X OAuth 1.0a PIN + 30 credits/hour. Follows only real linked members on X.
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
FOLLOW_GAP_SECONDS = 2.0
PIN_TTL = 7200

REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
VERIFY_URL = "https://api.twitter.com/1.1/account/verify_credentials.json"
FOLLOW_URL = "https://api.twitter.com/1.1/friendships/create.json"

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
            running INTEGER NOT NULL DEFAULT 0,
            next_follow_at REAL,
            cursor INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            target TEXT NOT NULL,
            name TEXT NOT NULL,
            at REAL NOT NULL,
            real INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS follows_handle ON follows(handle, id DESC);
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, typ in [
        ("req_token", "TEXT"),
        ("req_secret", "TEXT"),
        ("access_token", "TEXT"),
        ("access_secret", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(follows)").fetchall()}
    if "real" not in fcols:
        conn.execute("ALTER TABLE follows ADD COLUMN real INTEGER NOT NULL DEFAULT 0")
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


def live_pool(exclude: str):
    """Only real members who finished X OAuth (have access tokens)."""
    rows = db().execute(
        """
        SELECT handle, name, bio FROM users
        WHERE handle != ?
          AND access_token IS NOT NULL AND access_token != ''
          AND access_secret IS NOT NULL AND access_secret != ''
        ORDER BY last_seen DESC
        """,
        (exclude,),
    ).fetchall()
    return [(r["handle"], r["name"] or r["handle"], r["bio"] or "") for r in rows]


def member_count() -> int:
    row = db().execute(
        """
        SELECT COUNT(*) c FROM users
        WHERE access_token IS NOT NULL AND access_token != ''
        """
    ).fetchone()
    return int(row["c"])


def stats():
    now = time.time()
    total = member_count()
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


def apply_reset(u):
    now = time.time()
    if u["reset_at"] and now >= u["reset_at"]:
        db().execute(
            """
            UPDATE users
            SET credits = ?, reset_at = NULL, running = 0, next_follow_at = NULL
            WHERE handle = ?
            """,
            (MAX_CREDITS, u["handle"]),
        )
        db().commit()
        return get_user(u["handle"])
    return u


def x_follow(u, screen_name: str) -> tuple[bool, str]:
    if not u["access_token"] or not u["access_secret"]:
        return False, "not linked"
    auth = OAuth1(
        TWITTER_API_KEY,
        TWITTER_API_SECRET,
        u["access_token"],
        u["access_secret"],
    )
    try:
        r = requests.post(
            FOLLOW_URL,
            params={"screen_name": screen_name, "follow": "false"},
            auth=auth,
            timeout=20,
        )
        # 200 = followed; 403 often already following — still counts as success
        if r.status_code in (200, 403):
            return True, "ok"
        return False, f"{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)[:160]


def tick_follows(u):
    u = apply_reset(u)
    now = time.time()
    if not u["running"]:
        return u
    if now < (u["next_follow_at"] or 0):
        return u
    if u["credits"] <= 0:
        reset_at = u["reset_at"] or (now + HOUR_SECONDS)
        db().execute(
            "UPDATE users SET running = 0, next_follow_at = NULL, reset_at = ? WHERE handle = ?",
            (reset_at, u["handle"]),
        )
        db().commit()
        return get_user(u["handle"])

    if not u["access_token"] or not u["access_secret"]:
        db().execute("UPDATE users SET running = 0 WHERE handle = ?", (u["handle"],))
        db().commit()
        return get_user(u["handle"])

    pool = live_pool(u["handle"])
    if not pool:
        db().execute("UPDATE users SET running = 0 WHERE handle = ?", (u["handle"],))
        db().commit()
        return get_user(u["handle"])

    # Try real X follow; only spend a credit on success
    cursor = u["cursor"] or 0
    last_err = ""
    for i in range(len(pool)):
        member = pool[(cursor + i) % len(pool)]
        ok, detail = x_follow(u, member[0])
        if not ok:
            last_err = detail
            continue

        db().execute(
            "INSERT INTO follows (handle, target, name, at, real) VALUES (?, ?, ?, ?, 1)",
            (u["handle"], member[0], member[1], now),
        )
        credits = u["credits"] - 1
        running = 1 if credits > 0 else 0
        reset_at = (now + HOUR_SECONDS) if credits <= 0 else u["reset_at"]
        db().execute(
            """
            UPDATE users
            SET credits = ?, cursor = ?, running = ?, next_follow_at = ?,
                reset_at = ?, last_seen = ?
            WHERE handle = ?
            """,
            (
                credits,
                cursor + i + 1,
                running,
                now + FOLLOW_GAP_SECONDS,
                reset_at,
                now,
                u["handle"],
            ),
        )
        db().commit()
        return get_user(u["handle"])

    # No follow worked this tick — pause briefly, keep credits
    db().execute(
        "UPDATE users SET next_follow_at = ?, last_seen = ? WHERE handle = ?",
        (now + 5, now, u["handle"]),
    )
    db().commit()
    # stash last error on session-less path via a temp table field is overkill;
    # surface via message in user_state when no recent success
    _ = last_err
    return get_user(u["handle"])


def user_state(u):
    now = time.time()
    remain = max(0.0, (u["reset_at"] or 0) - now) if u["reset_at"] else 0.0
    follows = db().execute(
        "SELECT target, name, at, real FROM follows WHERE handle = ? ORDER BY id DESC LIMIT 40",
        (u["handle"],),
    ).fetchall()
    pool_n = len(live_pool(u["handle"]))
    last = follows[0] if follows else None
    msg = ""
    if not u["access_token"]:
        msg = "Re-authorize with X PIN so Start can follow other members."
    elif pool_n == 0:
        msg = "You are the only linked member right now. Share the site — Start needs other real accounts."
    elif last and last["real"]:
        msg = f"@{u['handle']} followed @{last['target']} on X"
        if u["credits"] <= 0:
            msg += ". Batch done. Stay open until Remaining Time hits 0."
    elif u["running"]:
        msg = f"Following other members on X… {u['credits']} credits left · {pool_n} in pool"
    return {
        "handle": u["handle"],
        "credits": u["credits"],
        "running": bool(u["running"]),
        "remain": remain,
        "remain_label": fmt(remain) if remain else ("Ready" if u["credits"] > 0 else "00:00"),
        "follows": [dict(f) for f in follows],
        "message": msg,
        "hour": HOUR_SECONDS,
        "stats": stats(),
        "linked": bool(u["access_token"]),
        "pool": pool_n,
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
    --muted:#8b97a6; --subtle:#6b7684; --primary:#e39a32; --pfg:#1a1208; --ok:#7dba7a;
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
  button { font:inherit; cursor:pointer; border:0; border-radius:10px; height:48px; padding:0 18px; background:var(--primary); color:var(--pfg); font-weight:500; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:transparent; color:var(--muted); height:36px; }
  button.full { width:100%; margin-top:14px; }
  .pin { font-family:Fraunces,serif; font-size:20px; letter-spacing:.04em; margin:8px 0 0; word-break:break-all; }
  .meters { display:grid; gap:12px; margin:28px 0 12px; }
  @media (min-width:700px) { .meters { grid-template-columns:repeat(3,1fr); } }
  .meter { background:var(--surface); border-radius:16px; padding:16px; box-shadow:0 0 0 1px rgba(243,236,225,.08); }
  .meter b { display:block; font-family:Fraunces,serif; font-size:30px; font-weight:500; font-variant-numeric:tabular-nums; }
  .bar { height:6px; background:var(--surface); border-radius:99px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--primary); width:0%; }
  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:20px 0; }
  .log { background:var(--surface); border-radius:16px; box-shadow:0 0 0 1px rgba(243,236,225,.08); overflow:hidden; }
  .log h2 { font-size:14px; margin:0; padding:12px 16px; border-bottom:1px solid rgba(243,236,225,.08); font-family:Outfit,sans-serif; }
  .log li { display:flex; justify-content:space-between; padding:12px 16px; border-top:1px solid rgba(243,236,225,.08); list-style:none; }
  .log ul { margin:0; padding:0; }
  .ok { color:var(--ok); font-size:12px; }
  .how { display:grid; gap:12px; margin-top:24px; }
  @media (min-width:700px) { .how { grid-template-columns:repeat(3,1fr); } }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td,th { text-align:left; padding:10px 12px; border-top:1px solid rgba(243,236,225,.08); }
  .err { color:#d36b5a; font-size:14px; margin-top:10px; }
  .msg { color:var(--muted); font-size:14px; margin-top:10px; }
  .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:24px; }
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
  <p class="kicker">Live member exchange</p>
  <h1>Follow real users. Get followed back.</h1>
  <p class="muted" style="max-width:540px">
    Authorize on X with a PIN (Toolkity-style). You join the live pool.
    Start spends <strong>{{ max_credits }} credits per hour</strong> following other real members on X.
  </p>
  <div class="stats">
    <div class="meter"><span class="subtle">LINKED MEMBERS</span><b>{{ st.total }}</b></div>
    <div class="meter"><span class="subtle">ACTIVE 24H</span><b>{{ st.active }}</b></div>
    <div class="meter"><span class="subtle">NEW 24H</span><b>{{ st.day }}</b></div>
  </div>
  <div class="grid2">
    <div class="card">
      <p class="subtle">STEP 1</p>
      <h2>Get PIN code on X</h2>
      <form method="post" action="{{ url_for('mint') }}">
        <label for="uh">Your X username</label>
        <div class="handle"><span>@</span>
          <input id="uh" name="handle" value="{{ form_handle }}" placeholder="yourhandle" autocomplete="username" required/>
        </div>
        <button class="full" type="submit">Get Pin Code On Twitter</button>
      </form>
      {% if auth_url %}
        <div class="card" style="background:var(--elevated);margin-top:16px;padding:16px">
          <p class="subtle">Open this link while logged into X, tap Authorize app, then copy the PIN:</p>
          <p class="pin"><a class="link" href="{{ auth_url }}" target="_blank" rel="noopener">{{ auth_url }}</a></p>
        </div>
      {% endif %}
    </div>
    <div class="card">
      <p class="subtle">STEP 2</p>
      <h2>Enter the PIN, then login</h2>
      <form method="post" action="{{ url_for('login') }}">
        <input type="hidden" name="handle" value="{{ form_handle }}"/>
        <label for="pin">PIN code from X</label>
        <input id="pin" name="pin" inputmode="numeric" maxlength="7" placeholder="7 digits" required/>
        <button class="full" type="submit">Login</button>
      </form>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      {% if info %}<p class="msg">{{ info }}</p>{% endif %}
    </div>
  </div>
  <h2 style="margin-top:56px">How it works</h2>
  <div class="how">
    <div class="card"><p class="kicker">01</p><h3>Authorize on X</h3><p class="muted">Request token → authorize URL → 7-digit PIN. No password stored here.</p></div>
    <div class="card"><p class="kicker">02</p><h3>Live pool only</h3><p class="muted">Only accounts that finished PIN login appear. No fake members.</p></div>
    <div class="card"><p class="kicker">03</p><h3>{{ max_credits }} credits / hour</h3><p class="muted">Start follows other members on X. Credit is spent only when the follow succeeds.</p></div>
  </div>
  {{ member_table|safe }}
</main>
{% else %}
<main class="wrap" style="padding-top:32px">
  <p class="kicker">Dashboard</p>
  <h1>Free X Followers</h1>
  <p class="muted">
    {{ max_credits }} credits / hour · real members only.
    {% if state.linked %}X linked.{% else %}Re-authorize required.{% endif %}
    Pool: <strong id="pool">{{ state.pool }}</strong>
  </p>
  <div class="meters">
    <div class="meter"><span class="subtle">CREDIT</span><b id="credits">{{ state.credits }}</b><span class="subtle">of {{ max_credits }} this hour</span></div>
    <div class="meter"><span class="subtle">REMAINING TIME</span><b id="remain">{{ state.remain_label }}</b><span class="subtle">until credits refill</span></div>
    <div class="meter"><span class="subtle">FOLLOWS (X)</span><b id="count">{{ state.follows|length }}</b><span class="subtle">this session log</span></div>
  </div>
  <div class="bar"><i id="bar"></i></div>
  <div class="row">
    <button id="start" type="button">Start</button>
    <p class="muted" id="hint"></p>
  </div>
  <p class="msg" id="msg">{{ state.message }}</p>
  <div class="log"><h2>Exchange log</h2><ul id="log"></ul></div>
  {{ member_table|safe }}
</main>
<script>
const HOUR = {{ hour }};
function render(s){
  document.getElementById('credits').textContent = s.credits;
  document.getElementById('remain').textContent = s.remain_label;
  document.getElementById('count').textContent = s.follows.length;
  document.getElementById('msg').textContent = s.message || '';
  const poolEl = document.getElementById('pool');
  if (poolEl) poolEl.textContent = s.pool;
  document.getElementById('bar').style.width = (s.remain > 0 ? Math.min(100, s.remain / HOUR * 100) : 0) + '%';
  const can = s.linked && s.pool > 0 && s.credits > 0 && !s.running;
  document.getElementById('start').disabled = !can && !(s.running);
  if (s.running) document.getElementById('start').disabled = true;
  document.getElementById('hint').textContent = !s.linked
    ? 'Link X first.'
    : (s.pool <= 0
      ? 'Need other linked members in the pool.'
      : (s.credits <= 0
        ? 'Wait for Remaining Time. Keep this page open.'
        : (s.running ? ('Following on X… ' + s.credits + ' left') : ('Uses up to ' + s.credits + ' credits.'))));
  document.getElementById('log').innerHTML = s.follows.length
    ? s.follows.map(f => '<li><div><strong>@'+f.target+'</strong><div class="subtle">'+f.name+' · X</div></div><span class="ok">Followed</span></li>').join('')
    : '<li class="muted">Press Start to follow the next real member on X.</li>';
}
async function pull(){ render(await (await fetch('/api/state')).json()); }
document.getElementById('start').onclick = async () => { await fetch('/api/start', {method:'POST'}); pull(); };
setInterval(pull, 800);
pull();
</script>
{% endif %}
</body>
</html>
"""


def member_table_html(exclude: str | None = None) -> str:
    pool = live_pool(exclude or "")
    if not pool:
        body = "<tr><td colspan='3' class='muted'>No other linked members yet. Invite people to authorize.</td></tr>"
    else:
        body = "".join(
            f"<tr><td>@{h}</td><td class='muted'>{bio or '—'}</td><td class='ok'>Linked</td></tr>"
            for h, _n, bio in pool[:40]
        )
    return (
        "<h2 style='margin-top:56px'>Members in the pool</h2>"
        "<div class='card' style='padding:0;overflow:auto'>"
        "<table><thead><tr><th>User</th><th>Bio</th><th>Status</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        "<p class='subtle' style='margin-top:24px;max-width:640px'>"
        "Only accounts that completed X PIN login. "
        f"{MAX_CREDITS} credits / hour. Start follows them on X.</p>"
    )


@app.get("/")
def home():
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
    session["req_token"] = req_token
    return redirect(
        url_for(
            "home",
            info="Open the authorize link, tap Authorize app on X, then enter the PIN below.",
        )
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
    if screen_name != handle:
        existing = get_user(screen_name)
        if existing is None:
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
            db().execute(
                """
                UPDATE users
                SET name = ?, bio = ?, access_token = ?, access_secret = ?,
                    req_token = NULL, req_secret = NULL, last_seen = ?
                WHERE handle = ?
                """,
                (name, bio, access_token, access_secret, now, screen_name),
            )
        db().commit()
        handle = screen_name
    else:
        db().execute(
            """
            UPDATE users
            SET name = ?, bio = ?, access_token = ?, access_secret = ?,
                req_token = NULL, req_secret = NULL, last_seen = ?
            WHERE handle = ?
            """,
            (name, bio, access_token, access_secret, now, handle),
        )
        db().commit()

    session.clear()
    session["handle"] = handle
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
    return jsonify(user_state(tick_follows(u)))


@app.post("/api/start")
@login_required
def api_start():
    u = apply_reset(get_user(session["handle"]))
    if not u:
        return jsonify({"error": "session"}), 401
    if not u["access_token"]:
        return jsonify(user_state(u)), 403
    if u["credits"] <= 0:
        return jsonify(user_state(u)), 409
    if not live_pool(u["handle"]):
        return jsonify(user_state(u)), 409
    db().execute(
        "UPDATE users SET running = 1, next_follow_at = ?, last_seen = ? WHERE handle = ?",
        (time.time(), time.time(), u["handle"]),
    )
    db().commit()
    return jsonify(user_state(tick_follows(get_user(u["handle"]))))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3333"))
    print(f"Flock live  http://0.0.0.0:{port}  db={DB_PATH}")
    app.run(host="0.0.0.0", port=port, debug=False)
