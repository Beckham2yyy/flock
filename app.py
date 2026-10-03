#!/usr/bin/env python3
"""
Flock Phase 1 — live site, simulated follows.

Real: usernames, sessions, PIN login, credits, hourly reset, member pool.
Not live: X OAuth / real follows (that's Phase 2).

  pip install flask
  FLOCK_SECRET='a-long-random-string' python3 app.py

  Demo hour: FLOCK_HOUR=80 python3 app.py
  Production: gunicorn -b 0.0.0.0:3333 -w 1 app:app
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, redirect, render_template_string, request, session, url_for

SECRET_KEY = os.environ.get("FLOCK_SECRET", "dev-only-change-me")
MAX_CREDITS = 10
HOUR_SECONDS = int(os.environ.get("FLOCK_HOUR", "3600"))
FOLLOW_GAP_SECONDS = 1.2
PIN_TTL = 7200
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flock.db")

# Shown until real people sign up. New logins join the live pool.
SEED = [
    ("mira_field", "Mira Field", "Design systems · slow tech"),
    ("northlane", "North Lane", "Markets, maps, morning notes"),
    ("oakandwire", "Oak & Wire", "Building in public"),
    ("sableloop", "Sable Loop", "Photo walks · city light"),
    ("kito_labs", "Kito Labs", "Tiny tools for writers"),
    ("reedlines", "Reed Lines", "Essays on attention"),
    ("halo_arc", "Halo Arc", "Product craft"),
    ("yen_notes", "Yen Notes", "Language & travel"),
    ("lowtide", "Low Tide", "Coastal studios"),
    ("paperkiln", "Paper Kiln", "Print & type"),
    ("driftrow", "Drift Row", "Indie games"),
    ("solace_io", "Solace", "Quiet software"),
]

app = Flask(__name__)
app.secret_key = SECRET_KEY


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            handle TEXT PRIMARY KEY,
            name TEXT,
            bio TEXT,
            pin TEXT,
            pin_at REAL,
            credits INTEGER NOT NULL DEFAULT 10,
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
            at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS follows_handle ON follows(handle, id DESC);
        """
    )
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


def mint_pin() -> str:
    return str(random.randint(1_000_000, 9_999_999))


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
    """Real signups first; seed fills the list so Start always has targets."""
    rows = db().execute(
        "SELECT handle, name, bio FROM users WHERE handle != ? ORDER BY last_seen DESC",
        (exclude,),
    ).fetchall()
    seen = {r["handle"] for r in rows}
    out = [(r["handle"], r["name"] or r["handle"], r["bio"] or "") for r in rows]
    for h, n, b in SEED:
        if h != exclude and h not in seen:
            out.append((h, n, b))
    return out


def stats():
    now = time.time()
    total = db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    active = db().execute(
        "SELECT COUNT(*) c FROM users WHERE last_seen > ?",
        (now - 86400,),
    ).fetchone()["c"]
    day = db().execute(
        "SELECT COUNT(*) c FROM users WHERE created_at > ?",
        (now - 86400,),
    ).fetchone()["c"]
    return {
        "total": total + len(SEED),
        "active": max(active, 1),
        "day": max(day, 0),
    }


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

    pool = live_pool(u["handle"])
    if not pool:
        db().execute("UPDATE users SET running = 0 WHERE handle = ?", (u["handle"],))
        db().commit()
        return get_user(u["handle"])

    member = pool[u["cursor"] % len(pool)]
    db().execute(
        "INSERT INTO follows (handle, target, name, at) VALUES (?, ?, ?, ?)",
        (u["handle"], member[0], member[1], now),
    )
    credits = u["credits"] - 1
    running = 1 if credits > 0 else 0
    reset_at = (now + HOUR_SECONDS) if credits <= 0 else u["reset_at"]
    db().execute(
        """
        UPDATE users
        SET credits = ?, cursor = ?, running = ?, next_follow_at = ?, reset_at = ?, last_seen = ?
        WHERE handle = ?
        """,
        (
            credits,
            u["cursor"] + 1,
            running,
            now + FOLLOW_GAP_SECONDS,
            reset_at,
            now,
            u["handle"],
        ),
    )
    db().commit()
    return get_user(u["handle"])


def user_state(u):
    now = time.time()
    remain = max(0.0, (u["reset_at"] or 0) - now) if u["reset_at"] else 0.0
    follows = db().execute(
        "SELECT target, name, at FROM follows WHERE handle = ? ORDER BY id DESC LIMIT 40",
        (u["handle"],),
    ).fetchall()
    last = follows[0] if follows else None
    msg = ""
    if last:
        msg = f"{u['handle']} followed @{last['target']}"
        if u["credits"] <= 0:
            msg += ". Batch done. Stay on this page until Remaining Time hits 0."
    elif u["running"]:
        msg = "Start — following fellow members from the exchange."
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
  .pin { font-family:Fraunces,serif; font-size:32px; letter-spacing:.2em; margin:8px 0 0; }
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
  <p class="kicker">Member exchange · Phase 1</p>
  <h1>Follow fellow users. Get followed back.</h1>
  <p class="muted" style="max-width:540px">
    Your handle is stored on this server. After PIN login you join the live member pool.
    Start spends 10 credits per hour following other members. Follows are still simulated until Phase 2 (X OAuth).
  </p>
  <div class="stats">
    <div class="meter"><span class="subtle">TOTAL MEMBERS</span><b>{{ st.total }}</b></div>
    <div class="meter"><span class="subtle">ACTIVE</span><b>{{ st.active }}</b></div>
    <div class="meter"><span class="subtle">LAST 24 HOURS</span><b>{{ st.day }}</b></div>
  </div>
  <div class="grid2">
    <div class="card">
      <p class="subtle">STEP 1</p>
      <h2>Get a PIN</h2>
      <form method="post" action="{{ url_for('mint') }}">
        <label for="uh">Your X username</label>
        <div class="handle"><span>@</span>
          <input id="uh" name="handle" value="{{ form_handle }}" placeholder="yourhandle" autocomplete="username" required/>
        </div>
        <button class="full" type="submit">Get PIN code</button>
      </form>
      {% if issued_pin %}
        <div class="card" style="background:var(--elevated);margin-top:16px;padding:16px">
          <p class="subtle">Your PIN (expires in 2 hours)</p>
          <p class="pin">{{ issued_pin }}</p>
        </div>
      {% endif %}
    </div>
    <div class="card">
      <p class="subtle">STEP 2</p>
      <h2>Enter PIN, then login</h2>
      <form method="post" action="{{ url_for('login') }}">
        <input type="hidden" name="handle" value="{{ form_handle }}"/>
        <label for="pin">PIN code</label>
        <input id="pin" name="pin" inputmode="numeric" maxlength="7" placeholder="7 digits" required/>
        <button class="full" type="submit">Login</button>
      </form>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      {% if info %}<p class="msg">{{ info }}</p>{% endif %}
    </div>
  </div>
  <h2 style="margin-top:56px">How it works</h2>
  <div class="how">
    <div class="card"><p class="kicker">01</p><h3>PIN session</h3><p class="muted">No X password. A 7-digit PIN binds this browser to your handle on the server.</p></div>
    <div class="card"><p class="kicker">02</p><h3>You join the pool</h3><p class="muted">After login you are a live member. Other people who Start will “follow” you in the exchange log.</p></div>
    <div class="card"><p class="kicker">03</p><h3>10 credits / hour</h3><p class="muted">Start spends credits. Remaining Time refills them. Keep the dashboard open.</p></div>
  </div>
  {{ member_table|safe }}
</main>
{% else %}
<main class="wrap" style="padding-top:32px">
  <p class="kicker">Dashboard</p>
  <h1>Free X Followers</h1>
  <p class="muted">Credits live on the server. Start follows other Flock members (simulated in Phase 1).</p>
  <div class="meters">
    <div class="meter"><span class="subtle">CREDIT</span><b id="credits">{{ state.credits }}</b><span class="subtle">of {{ max_credits }} this hour</span></div>
    <div class="meter"><span class="subtle">REMAINING TIME</span><b id="remain">{{ state.remain_label }}</b><span class="subtle">until credits refill</span></div>
    <div class="meter"><span class="subtle">FOLLOWS THIS SESSION</span><b id="count">{{ state.follows|length }}</b><span class="subtle">member-to-member</span></div>
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
  document.getElementById('bar').style.width = (s.remain > 0 ? Math.min(100, s.remain / HOUR * 100) : 0) + '%';
  document.getElementById('start').disabled = s.running || s.credits <= 0;
  document.getElementById('hint').textContent = s.credits <= 0
    ? 'Wait for Remaining Time. Keep this dashboard open.'
    : (s.running ? ('Following members… ' + s.credits + ' left') : ('Uses up to ' + s.credits + ' credits.'));
  document.getElementById('log').innerHTML = s.follows.length
    ? s.follows.map(f => '<li><div><strong>@'+f.target+'</strong><div class="subtle">'+f.name+'</div></div><span class="ok">Followed</span></li>').join('')
    : '<li class="muted">Press Start to follow the next member in the pool.</li>';
}
async function pull(){ render(await (await fetch('/api/state')).json()); }
document.getElementById('start').onclick = async () => { await fetch('/api/start', {method:'POST'}); pull(); };
setInterval(pull, 400);
pull();
</script>
{% endif %}
</body>
</html>
"""


def member_table_html(exclude: str | None = None) -> str:
    pool = live_pool(exclude or "")
    rows = "".join(
        f"<tr><td>@{h}</td><td class='muted'>{bio}</td><td class='ok'>Active</td></tr>"
        for h, _n, bio in pool[:24]
    )
    return (
        "<h2 style='margin-top:56px'>Members in the pool</h2>"
        "<div class='card' style='padding:0;overflow:auto'>"
        "<table><thead><tr><th>User</th><th>Description</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        "<p class='subtle' style='margin-top:24px;max-width:640px'>"
        "Phase 1: PIN, session, credits, and this pool are real on the server. "
        "Follows are recorded here only — X is not called. "
        f"Hour is {HOUR_SECONDS}s (FLOCK_HOUR=80 to demo).</p>"
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
                issued_pin=None,
                error=None,
                info=None,
                st=stats(),
            )
        session.clear()
    return render_template_string(
        PAGE,
        handle=None,
        form_handle=session.get("pending_handle", ""),
        issued_pin=session.get("issued_pin"),
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
    pin = mint_pin()
    upsert_user(handle)
    db().execute(
        "UPDATE users SET pin = ?, pin_at = ? WHERE handle = ?",
        (pin, time.time(), handle),
    )
    db().commit()
    session["pending_handle"] = handle
    session["issued_pin"] = pin
    return redirect(url_for("home", info="PIN ready. Enter it in step 2. You are in the member pool."))


@app.post("/login")
def login():
    handle = sanitize(request.form.get("handle") or session.get("pending_handle", ""))
    pin = "".join(c for c in (request.form.get("pin") or "") if c.isdigit())[:7]
    u = get_user(handle) if handle else None
    if not u:
        return redirect(url_for("home", error="Get a PIN first."))
    if not u["pin"] or pin != u["pin"]:
        return redirect(url_for("home", error="Wrong or expired PIN."))
    if u["pin_at"] and time.time() - u["pin_at"] > PIN_TTL:
        return redirect(url_for("home", error="PIN expired. Get a new one."))
    db().execute(
        "UPDATE users SET pin = NULL, last_seen = ? WHERE handle = ?",
        (time.time(), handle),
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
    if u["credits"] <= 0:
        return jsonify(user_state(u)), 409
    db().execute(
        "UPDATE users SET running = 1, next_follow_at = ?, last_seen = ? WHERE handle = ?",
        (time.time(), time.time(), u["handle"]),
    )
    db().commit()
    return jsonify(user_state(tick_follows(get_user(u["handle"]))))


app.permanent_session_lifetime = 60 * 60 * 24 * 30  # 30 days

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3333"))
    print(f"Flock Phase 1  http://0.0.0.0:{port}  hour={HOUR_SECONDS}s")
    if SECRET_KEY == "dev-only-change-me":
        print("WARNING: set FLOCK_SECRET before public deploy.")
    app.run(host="0.0.0.0", port=port, debug=False)
