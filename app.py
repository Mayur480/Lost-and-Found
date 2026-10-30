"""CampusFind - real Smart Campus Lost & Found website.
Single file: Flask + SQLite, real email verification (SMTP), photo uploads, claims, admin panel.
Run:  python app.py        Deploy:  gunicorn app:app
"""
import subprocess, sys
try:
    import flask  # noqa
except ImportError:  # first run: install Flask automatically
    print("Installing Flask (first run only)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])

import hashlib, hmac, os, re, secrets, smtplib, sqlite3, ssl, threading, time, webbrowser
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from email.message import EmailMessage
from functools import wraps
from jinja2 import DictLoader
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))

def load_env():  # tiny .env reader (no extra package needed)
    p = os.path.join(BASE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_env()
env = lambda k, d="": os.environ.get(k, d)

# ======================= SETTINGS (set these in .env) =======================
BASE_URL = env("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
PRODUCTION = BASE_URL.startswith("https")
DOMAINS = [d.strip().lower().lstrip("@") for d in env("ALLOWED_DOMAINS", "poornima.edu.in,poornima.org").split(",") if d.strip()]  # "*" = any email
ADMINS = [e.strip().lower() for e in env("ADMIN_EMAILS", "").split(",") if e.strip()]
SMTP = dict(host=env("SMTP_HOST"), port=int(env("SMTP_PORT", "587")), user=env("SMTP_USER"),
            pw=env("SMTP_PASSWORD"), sender=env("MAIL_FROM") or env("SMTP_USER"))
SEED_DEMO = env("SEED_DEMO", "0" if PRODUCTION else "1") == "1"
DB_PATH = env("DB_PATH", os.path.join(BASE, "lostfound.db"))
UPLOADS = env("UPLOAD_DIR", os.path.join(BASE, "static", "uploads"))
HERO_IMAGE = env("HERO_IMAGE", "/static/img/campus.jpg")  # big photo on the home screen (path or https link)
# ===========================================================================

def secret_key():
    if env("SECRET_KEY"):
        return env("SECRET_KEY")
    p = os.path.join(BASE, ".secret_key")  # generated once so logins survive restarts
    if not os.path.exists(p):
        open(p, "w").write(secrets.token_hex(32))
    return open(p).read().strip()

app = Flask(__name__)
app.config.update(SECRET_KEY=secret_key(), MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PRODUCTION)
if PRODUCTION:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CATEGORIES = ["Electronics", "Books", "ID & Cards", "Keys", "Bags", "Clothing", "Wallet", "Other"]
LOCATIONS = ["Main Library", "Student Center", "Canteen", "Parking Lot", "Auditorium", "Hostel", "Labs", "Sports Ground", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, pw TEXT, is_admin INTEGER DEFAULT 0, verified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, title TEXT, description TEXT,
  category TEXT, location TEXT, date TEXT, secret TEXT, status TEXT DEFAULT 'open', created TEXT, resolved TEXT);
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, item_id INTEGER, filename TEXT);
CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY, item_id INTEGER, user_id INTEGER, proof TEXT, score INTEGER,
  status TEXT DEFAULT 'pending', created TEXT);
CREATE TABLE IF NOT EXISTS codes(id INTEGER PRIMARY KEY, user_id INTEGER, purpose TEXT, code_hash TEXT, expires TEXT, tries INTEGER DEFAULT 0, created TEXT);
"""
ITEM = "SELECT items.*, (SELECT filename FROM photos WHERE item_id=items.id LIMIT 1) AS photo FROM items"


# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows

def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid

def now():
    return datetime.now().isoformat(timespec="seconds")

def init_db():
    os.makedirs(UPLOADS, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    if "verified" not in [r[1] for r in con.execute("PRAGMA table_info(users)")]:  # upgrade older database
        con.execute("ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0")
        con.execute("UPDATE users SET verified=1")
    if SEED_DEMO and not con.execute("SELECT 1 FROM users").fetchone():
        for n, e, p, a in [("Admin", "admin@poornima.edu.in", "admin123", 1), ("Demo Student", "demo@poornima.edu.in", "demo123", 0)]:
            con.execute("INSERT INTO users(name,email,pw,is_admin,verified) VALUES(?,?,?,?,1)", (n, e, generate_password_hash(p), a))
        d = date.today().isoformat()
        for t, ti, c, l, s in [("lost", "lappy", "Electronics", "Student Center", ""), ("lost", "book", "Books", "Main Library", ""),
                               ("found", "headphones", "Electronics", "Student Center", "blue case radiohead sticker"),
                               ("lost", "Air pod", "Other", "Parking Lot", "")]:
            con.execute("INSERT INTO items(user_id,type,title,description,category,location,date,secret,created) VALUES(2,?,?,?,?,?,?,?,?)",
                        (t, ti, "Sample item.", c, l, d, s, now()))
    con.commit()
    con.close()


# ---------- email ----------
def send_mail(to, subject, body):
    """Sends a real email through SMTP (background thread). Without SMTP settings it prints to the terminal."""
    if not SMTP["host"]:
        print(f"\n[DEV EMAIL - SMTP not configured]\nTo: {to}\nSubject: {subject}\n\n{body}\n")
        return
    def work():
        try:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = SMTP["sender"], to, subject
            m.set_content(body)
            ctx = ssl.create_default_context()
            if SMTP["port"] == 465:
                s = smtplib.SMTP_SSL(SMTP["host"], 465, context=ctx, timeout=20)
            else:
                s = smtplib.SMTP(SMTP["host"], SMTP["port"], timeout=20)
                s.starttls(context=ctx)
            with s:
                if SMTP["user"]:
                    s.login(SMTP["user"], SMTP["pw"])
                s.send_message(m)
        except Exception as e:  # never crash a request because of mail
            print("[EMAIL ERROR]", to, repr(e))
    threading.Thread(target=work, daemon=True).start()


# ---------- verification codes ----------
def hash_code(c):
    return hashlib.sha256((app.config["SECRET_KEY"] + c).encode()).hexdigest()

def issue_code(uid, purpose):
    code = f"{secrets.randbelow(10**6):06d}"
    ex("DELETE FROM codes WHERE user_id=? AND purpose=?", (uid, purpose))
    ex("INSERT INTO codes(user_id,purpose,code_hash,expires,created) VALUES(?,?,?,?,?)",
       (uid, purpose, hash_code(code), (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds"), now()))
    return code

def check_code(uid, purpose, code):
    r = q("SELECT * FROM codes WHERE user_id=? AND purpose=?", (uid, purpose), one=True)
    if not r or r["tries"] >= 5 or datetime.fromisoformat(r["expires"]) < datetime.now():
        return False
    ex("UPDATE codes SET tries=tries+1 WHERE id=?", (r["id"],))
    if hmac.compare_digest(r["code_hash"], hash_code(code)):
        ex("DELETE FROM codes WHERE id=?", (r["id"],))
        return True
    return False

def can_resend(uid, purpose):
    r = q("SELECT created FROM codes WHERE user_id=? AND purpose=?", (uid, purpose), one=True)
    return not r or (datetime.now() - datetime.fromisoformat(r["created"])).total_seconds() >= 60

def send_code(u, purpose):
    code = issue_code(u["id"], purpose)
    if purpose == "verify":
        send_mail(u["email"], "Verify your CampusFind account",
                  f"Hi {u['name']},\n\nYour CampusFind verification code is: {code}\n\nIt expires in 10 minutes. If you did not sign up, ignore this email.\n")
    else:
        send_mail(u["email"], "Reset your CampusFind password",
                  f"Hi {u['name']},\n\nYour password reset code is: {code}\n\nIt expires in 10 minutes. If you did not ask for this, ignore this email.\n")

HITS = {}
def limited(key, n=10, window=600):
    t = time.time()
    L = [x for x in HITS.get(key, []) if t - x < window] + [t]
    HITS[key] = L
    return len(L) > n

def valid_email(e):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)) and ("*" in DOMAINS or e.split("@")[1] in DOMAINS)

def domain_hint():
    return "any email address" if "*" in DOMAINS else "your college email (" + ", ".join("@" + d for d in DOMAINS) + ")"


# ---------- auto picture for items without a photo ----------
ICON_RULES = [  # (regex matched at the start of a word in the title, icon)
    (r"head ?phone|ear ?phone|ear ?bud|air ?pod|buds|headset|neckband", "🎧"),
    (r"laptop|lappy|macbook|notebook pc|chromebook", "💻"),
    (r"phone|mobile|iphone|android|smartphone", "📱"),
    (r"tablet|ipad", "📲"),
    (r"charger|cable|adapter|power ?bank", "🔌"),
    (r"keyboard|mouse", "⌨️"),
    (r"camera", "📷"),
    (r"watch|smartwatch", "⌚"),
    (r"calculator", "🧮"),
    (r"pen drive|pendrive|usb|flash drive|hard ?disk", "💾"),
    (r"book|notebook|copy|diary|novel|textbook", "📚"),
    (r"pen\b|pencil|marker|geometry", "✏️"),
    (r"key", "🔑"),
    (r"wallet|purse", "👛"),
    (r"bag|backpack|sack", "🎒"),
    (r"id card|student id|id\b|aadhaar|aadhar|license|licence|atm|debit|credit|card", "🪪"),
    (r"glasses|spectacles|specs|sunglass", "👓"),
    (r"umbrella", "☂️"),
    (r"bottle|flask|tiffin|lunch ?box", "🍶"),
    (r"jacket|hoodie|sweater|shirt|coat|cap|scarf", "🧥"),
    (r"shoe|sandal|slipper|sneaker", "👟"),
    (r"ring|bracelet|chain|necklace|earring", "💍"),
    (r"bike|cycle|helmet", "🚲"),
    (r"ball|bat|racket|racquet", "🏏"),
]
CATEGORY_ICONS = {"Electronics": "🔌", "Books": "📚", "ID & Cards": "🪪", "Keys": "🔑",
                  "Bags": "🎒", "Clothing": "🧥", "Wallet": "👛", "Other": "📦"}

def icon_for(title, category=""):
    t = (title or "").lower()
    for pattern, icon in ICON_RULES:
        if re.search(r"\b(?:" + pattern + ")", t):
            return icon
    return CATEGORY_ICONS.get(category, "📦")

app.jinja_env.globals["icon_for"] = icon_for


# ---------- request hooks ----------
@app.before_request
def load_user():
    g.user = q("SELECT * FROM users WHERE id=?", (session["uid"],), one=True) if session.get("uid") else None
    if request.method == "POST":  # CSRF check on every POST
        tok = request.form.get("csrf")
        if not tok or tok != session.get("csrf"):
            abort(400)

@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r

@app.errorhandler(413)
def too_big(_):
    return "Upload too large (max 25 MB in total, 5 MB per photo).", 413

@app.context_processor
def inject():
    session.setdefault("csrf", secrets.token_hex(16))
    pending = 0
    if g.user:
        pending = q("SELECT COUNT(*) c FROM claims JOIN items ON items.id=claims.item_id "
                    "WHERE items.user_id=? AND claims.status='pending'", (g.user["id"],), one=True)["c"]
    return dict(csrf=session["csrf"], me=g.user, pending=pending, CATEGORIES=CATEGORIES, LOCATIONS=LOCATIONS,
                today=date.today().isoformat(), domains=DOMAINS, dev=not SMTP["host"], hint=domain_hint())

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not g.user["is_admin"]:
            abort(403)
        return f(*a, **k)
    return w


# ---------- accounts ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:60]
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if limited("reg:" + str(request.remote_addr), 10, 3600):
            flash("Too many attempts. Try again later.", "error")
        elif not name or not valid_email(email):
            flash("Use " + domain_hint() + ".", "error")
        elif len(pw) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
            if u and u["verified"]:
                flash("That email is already registered. Please sign in.", "error")
            else:
                if u:
                    ex("UPDATE users SET name=?, pw=? WHERE id=?", (name, generate_password_hash(pw), u["id"]))
                else:
                    ex("INSERT INTO users(name,email,pw,is_admin,verified) VALUES(?,?,?,?,0)",
                       (name, email, generate_password_hash(pw), int(email in ADMINS)))
                send_code(q("SELECT * FROM users WHERE email=?", (email,), one=True), "verify")
                session["vemail"] = email
                return redirect(url_for("verify"))
    return render_template("auth.html", mode="register")

@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = (request.form.get("email") or session.get("vemail") or "").strip().lower()
    u = q("SELECT * FROM users WHERE email=?", (email,), one=True) if email else None
    if not u or u["verified"]:
        return redirect(url_for("login"))
    if request.method == "POST":
        if limited("ver:" + str(request.remote_addr), 20, 600):
            flash("Too many attempts. Wait a few minutes.", "error")
        elif request.form.get("resend"):
            if can_resend(u["id"], "verify"):
                send_code(u, "verify")
                flash("A new code was sent.")
            else:
                flash("Please wait a minute before asking for another code.", "error")
        elif check_code(u["id"], "verify", request.form.get("code", "").strip()):
            ex("UPDATE users SET verified=1 WHERE id=?", (u["id"],))
            session.clear()
            session["uid"] = u["id"]
            flash("Email verified. Welcome to CampusFind!")
            return redirect(url_for("home"))
        else:
            flash("Wrong or expired code.", "error")
    return render_template("verify.html", email=email)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),), one=True)
        if limited("log:" + str(request.remote_addr), 15, 600):
            flash("Too many attempts. Wait a few minutes.", "error")
        elif u and check_password_hash(u["pw"], request.form.get("password", "")):
            if not u["verified"]:
                session["vemail"] = u["email"]
                if can_resend(u["id"], "verify"):
                    send_code(u, "verify")
                flash("Please verify your email first. We sent you a code.")
                return redirect(url_for("verify"))
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        else:
            flash("Wrong email or password.", "error")
    return render_template("auth.html", mode="login")

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if limited("for:" + str(request.remote_addr), 8, 3600):
            flash("Too many attempts. Try again later.", "error")
        else:
            u = q("SELECT * FROM users WHERE email=? AND verified=1", (email,), one=True)
            if u and can_resend(u["id"], "reset"):
                send_code(u, "reset")
            session["remail"] = email
            flash("If that email has an account, a reset code is on its way.")
            return redirect(url_for("reset"))
    return render_template("forgot.html")

@app.route("/reset", methods=["GET", "POST"])
def reset():
    email = (request.form.get("email") or session.get("remail") or "").strip().lower()
    if request.method == "POST":
        pw = request.form.get("password", "")
        u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if limited("res:" + str(request.remote_addr), 20, 600):
            flash("Too many attempts. Wait a few minutes.", "error")
        elif len(pw) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif u and check_code(u["id"], "reset", request.form.get("code", "").strip()):
            ex("UPDATE users SET pw=?, verified=1 WHERE id=?", (generate_password_hash(pw), u["id"]))
            session.clear()
            flash("Password updated. Please sign in.")
            return redirect(url_for("login"))
        else:
            flash("Wrong or expired code.", "error")
    return render_template("reset.html", email=email)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------- public pages ----------
@app.route("/")
def home():
    stats = q("SELECT COUNT(*) total, COALESCE(SUM(type='lost'),0) lost, COALESCE(SUM(type='found'),0) found "
              "FROM items WHERE status='open'", one=True)
    return render_template("index.html", stats=stats, recent=q(ITEM + " WHERE status='open' ORDER BY created DESC LIMIT 8"))

@app.route("/browse")
def browse():
    f = {k: request.args.get(k, "").strip() for k in ("q", "type", "category", "location")}
    sql, args = ITEM + " WHERE items.status='open'", []
    if f["q"]:
        sql += " AND (title LIKE ? OR description LIKE ?)"
        args += ["%" + f["q"] + "%"] * 2
    for k in ("type", "category", "location"):
        if f[k]:
            sql += f" AND {k}=?"
            args.append(f[k])
    return render_template("browse.html", items=q(sql + " ORDER BY created DESC", args), f=f)

def similar(it, n=4):
    other = "found" if it["type"] == "lost" else "lost"
    rows = q(ITEM + " WHERE items.type=? AND items.status='open' AND items.id!=?", (other, it["id"]))
    score = lambda r: SequenceMatcher(None, it["title"].lower(), r["title"].lower()).ratio() + (0.3 if r["category"] == it["category"] else 0)
    return sorted(rows, key=score, reverse=True)[:n]

@app.route("/item/<int:item_id>")
def item_page(item_id):
    it = q("SELECT items.*, users.name AS owner, users.email AS owner_email FROM items JOIN users ON users.id=items.user_id WHERE items.id=?", (item_id,), one=True) or abort(404)
    photos = q("SELECT filename FROM photos WHERE item_id=?", (item_id,))
    mine = g.user and (g.user["id"] == it["user_id"] or g.user["is_admin"])
    claims = q("SELECT claims.*, users.name AS claimant, users.email AS cemail FROM claims JOIN users ON users.id=claims.user_id "
               "WHERE item_id=? ORDER BY created DESC", (item_id,)) if mine else []
    my_claim = q("SELECT * FROM claims WHERE item_id=? AND user_id=?", (item_id, g.user["id"]), one=True) if g.user else None
    return render_template("item.html", it=it, photos=photos, claims=claims, my_claim=my_claim, mine=mine, similar=similar(it))

@app.route("/uploads/<name>")
def uploaded(name):
    return send_from_directory(UPLOADS, name, max_age=86400)

@app.route("/assets/<name>")
def asset(name):
    if name == "style.css":
        return Response(CSS.replace("__HERO__", HERO_IMAGE), mimetype="text/css")
    if name == "app.js":
        return Response(JS, mimetype="text/javascript")
    abort(404)


# ---------- actions ----------
def image_type(b):
    """Detect real image type from file bytes (not from the file name)."""
    if b.startswith(b"\xff\xd8\xff"): return "jpg"
    if b.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    return None

def match_score(secret, proof):
    words = {w for w in re.findall(r"[a-z0-9]+", (secret or "").lower()) if len(w) > 3}
    if not words:
        return None
    return round(100 * len(words & set(re.findall(r"[a-z0-9]+", proof.lower()))) / len(words))

def save_photos(iid, files):
    """Save up to 5 valid images per item. Returns (saved, skipped)."""
    have = q("SELECT COUNT(*) c FROM photos WHERE item_id=?", (iid,), one=True)["c"]
    saved = skipped = 0
    for file in files:
        if not file or not file.filename:
            continue
        if have + saved >= 5:
            skipped += 1
            continue
        data = file.read(5 * 1024 * 1024 + 1)
        ext = image_type(data[:12])
        if not ext or len(data) > 5 * 1024 * 1024:
            skipped += 1
            continue
        name = secrets.token_hex(8) + "." + ext
        with open(os.path.join(UPLOADS, name), "wb") as out:
            out.write(data)
        ex("INSERT INTO photos(item_id,filename) VALUES(?,?)", (iid, name))
        saved += 1
    return saved, skipped

@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    if request.method == "POST":
        f = request.form
        typ = f.get("type") if f.get("type") in ("lost", "found") else "lost"
        title = f.get("title", "").strip()[:100]
        if not title or f.get("category") not in CATEGORIES or f.get("location") not in LOCATIONS:
            flash("Please fill in the title, category and location.", "error")
            return render_template("report.html", form=f, type=typ), 400
        try:
            d = date.fromisoformat(f.get("date") or date.today().isoformat())
        except ValueError:
            d = date.today()
        iid = ex("INSERT INTO items(user_id,type,title,description,category,location,date,secret,created) VALUES(?,?,?,?,?,?,?,?,?)",
                 (g.user["id"], typ, title, f.get("description", "").strip()[:600], f["category"], f["location"],
                  d.isoformat(), f.get("secret", "").strip()[:200] if typ == "found" else "", now()))
        saved, skipped = save_photos(iid, request.files.getlist("photos"))
        flash("Your item has been posted." + (f" {skipped} file(s) skipped (not a valid image under 5 MB)." if skipped else ""))
        return redirect(url_for("item_page", item_id=iid))
    return render_template("report.html", form={}, type="found" if request.args.get("type") == "found" else "lost")

@app.post("/item/<int:item_id>/photos")
@login_required
def add_photos(item_id):
    it = q("SELECT * FROM items WHERE id=?", (item_id,), one=True) or abort(404)
    if not (g.user["is_admin"] or it["user_id"] == g.user["id"]):
        abort(403)
    saved, skipped = save_photos(item_id, request.files.getlist("photos"))
    if saved:
        flash(f"{saved} photo(s) added." + (f" {skipped} skipped." if skipped else ""))
    else:
        flash("No photo added. Use a JPG, PNG, GIF or WebP under 5 MB (max 5 photos per item).", "error")
    return redirect(url_for("item_page", item_id=item_id))

@app.post("/item/<int:item_id>/claim")
@login_required
def claim(item_id):
    it = q("SELECT * FROM items WHERE id=?", (item_id,), one=True) or abort(404)
    if it["status"] != "open" or it["user_id"] == g.user["id"]:
        abort(400)
    proof = request.form.get("proof", "").strip()[:500]
    if q("SELECT 1 FROM claims WHERE item_id=? AND user_id=?", (item_id, g.user["id"]), one=True):
        flash("You already sent a claim for this item.", "error")
    elif len(proof) < 10:
        flash("Please describe the item in a bit more detail (10+ characters).", "error")
    else:
        ex("INSERT INTO claims(item_id,user_id,proof,score,created) VALUES(?,?,?,?,?)",
           (item_id, g.user["id"], proof, match_score(it["secret"], proof), now()))
        owner = q("SELECT name,email FROM users WHERE id=?", (it["user_id"],), one=True)
        send_mail(owner["email"], f"New claim on your post: {it['title']}",
                  f"Hi {owner['name']},\n\n{g.user['name']} sent a claim for \"{it['title']}\".\n\nTheir message:\n{proof}\n\n"
                  f"Review it here: {BASE_URL}/item/{item_id}\n")
        flash("Claim sent. The poster will review it.")
    return redirect(url_for("item_page", item_id=item_id))

@app.post("/claim/<int:claim_id>/<action>")
@login_required
def decide(claim_id, action):
    c = q("SELECT claims.*, items.user_id AS owner, items.title FROM claims JOIN items ON items.id=claims.item_id WHERE claims.id=?", (claim_id,), one=True) or abort(404)
    if action not in ("approve", "reject") or not (g.user["is_admin"] or c["owner"] == g.user["id"]):
        abort(403)
    cl = q("SELECT name,email FROM users WHERE id=?", (c["user_id"],), one=True)
    ow = q("SELECT name,email FROM users WHERE id=?", (c["owner"],), one=True)
    if action == "approve":
        ex("UPDATE claims SET status='approved' WHERE id=?", (claim_id,))
        ex("UPDATE claims SET status='rejected' WHERE item_id=? AND id!=? AND status='pending'", (c["item_id"], claim_id))
        ex("UPDATE items SET status='returned', resolved=? WHERE id=?", (now(), c["item_id"]))
        send_mail(cl["email"], f"Your claim was approved: {c['title']}",
                  f"Hi {cl['name']},\n\nGood news! Your claim for \"{c['title']}\" was approved.\n"
                  f"Contact {ow['name']} at {ow['email']} to arrange the handover.\n")
        send_mail(ow["email"], f"Claim approved: {c['title']}",
                  f"Hi {ow['name']},\n\nThe claim by {cl['name']} ({cl['email']}) for \"{c['title']}\" was approved. "
                  f"Please arrange the handover with them.\n")
        flash("Claim approved. Both of you were emailed each other's contact.")
    else:
        ex("UPDATE claims SET status='rejected' WHERE id=?", (claim_id,))
        send_mail(cl["email"], f"Your claim was not approved: {c['title']}",
                  f"Hi {cl['name']},\n\nYour claim for \"{c['title']}\" was not approved. If you think this is a mistake, "
                  f"send a new description or contact the campus security desk.\n")
        flash("Claim rejected.")
    return redirect(request.referrer or url_for("dashboard"))

@app.post("/item/<int:item_id>/delete")
@login_required
def delete_item(item_id):
    it = q("SELECT * FROM items WHERE id=?", (item_id,), one=True) or abort(404)
    if not (g.user["is_admin"] or it["user_id"] == g.user["id"]):
        abort(403)
    for p in q("SELECT filename FROM photos WHERE item_id=?", (item_id,)):
        try:
            os.remove(os.path.join(UPLOADS, p["filename"]))
        except OSError:
            pass
    for t in ("photos", "claims"):
        ex(f"DELETE FROM {t} WHERE item_id=?", (item_id,))
    ex("DELETE FROM items WHERE id=?", (item_id,))
    flash("Item deleted.")
    return redirect(url_for("dashboard"))


# ---------- dashboards ----------
@app.route("/dashboard")
@login_required
def dashboard():
    uid = g.user["id"]
    return render_template(
        "dashboard.html",
        mine=q(ITEM + " WHERE items.user_id=? ORDER BY created DESC", (uid,)),
        my_claims=q("SELECT claims.*, items.title FROM claims JOIN items ON items.id=claims.item_id WHERE claims.user_id=? ORDER BY claims.created DESC", (uid,)),
        incoming=q("SELECT claims.*, items.title, users.name AS claimant FROM claims JOIN items ON items.id=claims.item_id "
                   "JOIN users ON users.id=claims.user_id WHERE items.user_id=? AND claims.status='pending'", (uid,)))

@app.route("/admin")
@admin_required
def admin():
    week = (datetime.now() - timedelta(days=7)).isoformat()
    stats = q("SELECT (SELECT COUNT(*) FROM items WHERE status='open') open_items, "
              "(SELECT COUNT(*) FROM items WHERE status='returned' AND resolved>=?) closed_week, "
              "(SELECT COUNT(*) FROM claims WHERE status='pending') pending, (SELECT COUNT(*) FROM users WHERE verified=1) users", (week,), one=True)
    pending = q("SELECT claims.*, items.title, users.name AS claimant, users.email FROM claims JOIN items ON items.id=claims.item_id "
                "JOIN users ON users.id=claims.user_id WHERE claims.status='pending' ORDER BY claims.created")
    return render_template("admin.html", stats=stats, pending=pending, items=q(ITEM + " ORDER BY created DESC LIMIT 50"))


# =================== HTML TEMPLATES, CSS, JAVASCRIPT ===================
TEMPLATES = {}

TEMPLATES["_card.html"] = r"""{% macro card(i) %}
<a class="card" href="{{ url_for('item_page', item_id=i.id) }}">
  <div class="thumb">
    {% if i.photo %}<img src="{{ url_for('uploaded', name=i.photo) }}" alt="">{% else %}<span class="auto-icon" title="Auto picture (no photo uploaded)">{{ icon_for(i.title, i.category) }}</span>{% endif %}
    <b class="tag {{ i.type }}">{{ i.type|capitalize }}</b>
  </div>
  <div class="body">
    <h3>{{ i.title }}</h3>
    <p class="muted">📍 {{ i.location }} &nbsp; 📅 {{ i.date }}</p>
    <span class="chip">{{ i.category }}</span>
  </div>
</a>
{% endmacro %}
"""

TEMPLATES["admin.html"] = r"""{% extends "base.html" %}
{% from "_card.html" import card %}
{% block title %}Admin · CampusFind{% endblock %}
{% block content %}
<section class="wrap">
  <h1>🛡 Admin Panel</h1>
  <p class="muted">Manage all aspects of the Campus Lost &amp; Found platform.</p>
  <div class="stats four">
    <div><b>{{ stats.open_items }}</b><span>OPEN ITEMS</span></div><div><b>{{ stats.closed_week }}</b><span>CLOSED THIS WEEK</span></div>
    <div><b>{{ stats.pending }}</b><span>PENDING CLAIMS</span></div><div><b>{{ stats.users }}</b><span>USERS</span></div>
  </div>
  <h2>Review pending claims</h2>
  {% for c in pending %}<div class="panel claim">
    <div><b>{{ c.claimant }}</b> ({{ c.email }}) on <a href="{{ url_for('item_page', item_id=c.item_id) }}">{{ c.title }}</a>
      {% if c.score is not none %}<span class="chip {{ 'good' if c.score >= 50 else 'warn' }}">{{ c.score }}% match</span>{% endif %}<p>{{ c.proof }}</p></div>
    <div class="row">{% for a in ('approve', 'reject') %}<form method="post" action="{{ url_for('decide', claim_id=c.id, action=a) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
      <button class="btn sm {{ 'primary' if a == 'approve' else 'danger' }}">{{ a|capitalize }}</button></form>{% endfor %}</div>
  </div>{% else %}<p class="muted">All clear.</p>{% endfor %}
  <h2>All items</h2>
  <div class="grid">{% for i in items %}{{ card(i) }}{% endfor %}</div>
</section>
{% endblock %}
"""

TEMPLATES["auth.html"] = r"""{% extends "base.html" %}
{% block title %}{{ 'Create account' if mode == 'register' else 'Sign in' }} · CampusFind{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>{{ 'Create your account' if mode == 'register' else 'Welcome back' }}</h1>
  <p class="muted">Use {{ hint }}. We email you a 6-digit code to confirm it is really yours.</p>
  <form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf }}">
    {% if mode == 'register' %}<label>Full name<input name="name" required maxlength="60"></label>{% endif %}
    <label>College email<input type="email" name="email" required placeholder="name@{{ domains[0] if domains[0] != '*' else 'example.com' }}"></label>
    <label>Password<input type="password" name="password" required minlength="6"></label>
    {% if mode == 'login' %}<p class="muted"><a href="{{ url_for('forgot') }}">Forgot password?</a></p>{% endif %}
    <button class="btn primary wide">{{ 'Create account' if mode == 'register' else 'Sign in' }}</button>
  </form>
  <p class="muted">{% if mode == 'register' %}Have an account? <a href="{{ url_for('login') }}">Sign in</a>{% else %}New here? <a href="{{ url_for('register') }}">Create an account</a>{% endif %}</p>
</section>
{% endblock %}
"""

TEMPLATES["base.html"] = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">                    
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}CampusFind{% endblock %}</title>
  <script>document.documentElement.dataset.theme = localStorage.getItem('theme') || 'light';</script>
  <link rel="stylesheet" href="{{ url_for('asset', name='style.css') }}">
</head>
<body>
<header class="top">
  <a class="brand" href="{{ url_for('home') }}"><span class="logo">⌕</span>CampusFind</a>
  <nav>
    <a href="{{ url_for('home') }}">Home</a>
    <a href="{{ url_for('browse') }}">Browse</a>
    {% if me %}<a href="{{ url_for('dashboard') }}">Dashboard</a>{% endif %}
    {% if me and me.is_admin %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
    <a class="btn primary sm" href="{{ url_for('report') }}">+ Report</a>
    {% if me %}
      <a class="icon" href="{{ url_for('dashboard') }}" title="Pending claims">🔔{% if pending %}<i class="dot">{{ pending }}</i>{% endif %}</a>
      <a class="icon" href="{{ url_for('logout') }}" title="Sign out ({{ me.name }})">👤</a>
    {% else %}
      <a href="{{ url_for('login') }}">Sign in</a>
    {% endif %}
    <button class="icon" id="theme" title="Toggle dark mode">🌓</button>
  </nav>
</header>
<main>
  {% for cat, msg in get_flashed_messages(with_categories=true) %}
    <div class="flash {{ cat }}">{{ msg }}</div>
  {% endfor %}
  {% block content %}{% endblock %}
</main>
<script src="{{ url_for('asset', name='app.js') }}"></script>
</body>
</html>
"""

TEMPLATES["browse.html"] = r"""{% extends "base.html" %}
{% from "_card.html" import card %}
{% block title %}Browse · CampusFind{% endblock %}
{% block content %}
<section class="wrap">
  <h1>Browse items</h1>
  <form class="filters" data-auto>
    <input name="q" placeholder="Search…" value="{{ f.q }}">
    <select name="type"><option value="">Lost &amp; Found</option>
      <option value="lost" {{ 'selected' if f.type=='lost' }}>Lost</option><option value="found" {{ 'selected' if f.type=='found' }}>Found</option></select>
    <select name="category"><option value="">All categories</option>{% for c in CATEGORIES %}<option {{ 'selected' if f.category==c }}>{{ c }}</option>{% endfor %}</select>
    <select name="location"><option value="">All locations</option>{% for l in LOCATIONS %}<option {{ 'selected' if f.location==l }}>{{ l }}</option>{% endfor %}</select>
    <button class="btn dark">Filter</button>
  </form>
  <div class="grid">{% for i in items %}{{ card(i) }}{% else %}<p class="muted">Nothing matches. Try fewer filters.</p>{% endfor %}</div>
</section>
{% endblock %}
"""

TEMPLATES["dashboard.html"] = r"""{% extends "base.html" %}
{% from "_card.html" import card %}
{% block title %}Dashboard · CampusFind{% endblock %}
{% block content %}
<section class="wrap">
  <h1>Hi, {{ me.name }}</h1>
  <h2>Claims waiting for you</h2>
  {% for c in incoming %}<div class="panel claim">
    <div><b>{{ c.claimant }}</b> on <a href="{{ url_for('item_page', item_id=c.item_id) }}">{{ c.title }}</a>
      {% if c.score is not none %}<span class="chip {{ 'good' if c.score >= 50 else 'warn' }}">{{ c.score }}% match</span>{% endif %}<p>{{ c.proof }}</p></div>
    <div class="row">{% for a in ('approve', 'reject') %}<form method="post" action="{{ url_for('decide', claim_id=c.id, action=a) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
      <button class="btn sm {{ 'primary' if a == 'approve' else 'danger' }}">{{ a|capitalize }}</button></form>{% endfor %}</div>
  </div>{% else %}<p class="muted">Nothing to review.</p>{% endfor %}
  <h2>My claims</h2>
  {% for c in my_claims %}<p><a href="{{ url_for('item_page', item_id=c.item_id) }}">{{ c.title }}</a> <span class="chip">{{ c.status }}</span></p>{% else %}<p class="muted">You haven’t claimed anything.</p>{% endfor %}
  <h2>My posts</h2>
  <div class="grid">{% for i in mine %}{{ card(i) }}{% else %}<p class="muted">No posts yet.</p>{% endfor %}</div>
</section>
{% endblock %}
"""

TEMPLATES["index.html"] = r"""{% extends "base.html" %}
{% from "_card.html" import card %}
{% block content %}
<section class="hero">
  <h1>Lost something?<br>We’ll help you find it.</h1>
  <p>Report lost or found items and reconnect your campus community with their belongings.</p>
  <form action="{{ url_for('browse') }}" class="search"><input name="q" placeholder="Search for items…"><button class="btn dark">Search</button></form>
  <div class="row"><a class="btn ghost-w" href="{{ url_for('report', type='lost') }}">I Lost Something</a>
  <a class="btn ghost-w" href="{{ url_for('report', type='found') }}">I Found Something</a></div>
</section>
<section class="stats">
  <div><b>{{ stats.total }}</b><span>TOTAL ITEMS</span></div>
  <div><b>{{ stats.lost }}</b><span>LOST</span></div>
  <div><b>{{ stats.found }}</b><span>FOUND</span></div>
</section>
<section class="wrap">
  <div class="between"><h2>Recent Items</h2><a href="{{ url_for('browse') }}">View All →</a></div>
  <div class="grid">{% for i in recent %}{{ card(i) }}{% else %}<p class="muted">No items yet. Be the first to report one.</p>{% endfor %}</div>
</section>
{% endblock %}
"""

TEMPLATES["item.html"] = r"""{% extends "base.html" %}
{% from "_card.html" import card %}
{% block title %}{{ it.title }} · CampusFind{% endblock %}
{% block content %}
<section class="wrap">
  <a href="{{ url_for('browse') }}" class="muted">← Back to browse</a>
  <div class="detail">
    <div class="gallery">
      {% for p in photos %}<img src="{{ url_for('uploaded', name=p.filename) }}" alt="Photo of {{ it.title }}">
      {% else %}<div class="noimg">{{ icon_for(it.title, it.category) }}</div>{% endfor %}
    </div>
    <div>
      <span class="tag {{ it.type }} static">{{ it.type|capitalize }}</span>
      {% if it.status == 'returned' %}<span class="tag found static">Returned</span>{% endif %}
      <h1>{{ it.title }}</h1>
      <p>{{ it.description or 'No description.' }}</p>
      <dl class="facts">
        <div><dt>Category</dt><dd>{{ it.category }}</dd></div><div><dt>Location</dt><dd>{{ it.location }}</dd></div>
        <div><dt>Date</dt><dd>{{ it.date }}</dd></div><div><dt>Posted by</dt><dd>{{ it.owner }}</dd></div>
      </dl>
      {% if my_claim and my_claim.status == 'approved' %}<p class="note">Claim approved. Contact {{ it.owner }}: <b>{{ it.owner_email }}</b></p>{% endif %}
      {% if it.status == 'open' %}
        {% if not me %}<a class="btn primary wide" href="{{ url_for('login', next=request.path) }}">Sign in to claim</a>
        {% elif me.id != it.user_id %}
          {% if my_claim %}<p class="note">Your claim is <b>{{ my_claim.status }}</b>.</p>
          {% else %}<button class="btn primary wide" data-open="claimBox">{{ 'This is mine — Claim item' if it.type == 'found' else 'I found this item' }}</button>{% endif %}
        {% endif %}
      {% endif %}
      {% if mine %}
        <form method="post" action="{{ url_for('add_photos', item_id=it.id) }}" enctype="multipart/form-data" class="form">
          <input type="hidden" name="csrf" value="{{ csrf }}">
          <label>Add photos<input type="file" name="photos" accept="image/*" multiple required></label>
          <button class="btn sm">Upload photo</button>
        </form>
        <form method="post" action="{{ url_for('delete_item', item_id=it.id) }}" onsubmit="return confirm('Delete this item?')">
          <input type="hidden" name="csrf" value="{{ csrf }}"><button class="btn danger sm">Delete post</button></form>
      {% endif %}
    </div>
  </div>

  {% if claims %}
  <h2>Claims</h2>
  {% for c in claims %}<div class="panel claim">
    <div><b>{{ c.claimant }}</b> <span class="chip">{{ c.status }}</span>{% if c.status == 'approved' %} <span class="chip good">{{ c.cemail }}</span>{% endif %}
      {% if c.score is not none %}<span class="chip {{ 'good' if c.score >= 50 else 'warn' }}">{{ c.score }}% match</span>{% endif %}
      <p>{{ c.proof }}</p></div>
    {% if c.status == 'pending' and it.status == 'open' %}<div class="row">
      {% for a in ('approve', 'reject') %}<form method="post" action="{{ url_for('decide', claim_id=c.id, action=a) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
        <button class="btn sm {{ 'primary' if a == 'approve' else 'danger' }}">{{ a|capitalize }}</button></form>{% endfor %}</div>{% endif %}
  </div>{% endfor %}
  {% endif %}

  <h2>Similar items</h2>
  <div class="grid">{% for s in similar %}{{ card(s) }}{% else %}<p class="muted">No similar items yet.</p>{% endfor %}</div>
</section>

<dialog id="claimBox">
  <form method="post" action="{{ url_for('claim', item_id=it.id) }}">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <h3>{{ 'Claim This Item' if it.type == 'found' else 'Tell the owner' }}</h3>
    <p class="muted">{{ 'Provide a brief description to prove this item belongs to you. Be specific about identifying features.' if it.type == 'found' else 'Describe where and when you found it.' }}</p>
    <label>{{ 'Proof of ownership' if it.type == 'found' else 'Message' }}
      <textarea name="proof" rows="4" maxlength="500" required data-count="#cnt" placeholder="e.g., It’s a blue case with a sticker on the back"></textarea></label>
    <small id="cnt">0/500</small>
    <div class="row end"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary">Submit Claim</button></div>
  </form>
</dialog>
{% endblock %}
"""

TEMPLATES["report.html"] = r"""{% extends "base.html" %}
{% block title %}Report an item · CampusFind{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Report an Item</h1>
  <p class="muted">Help your campus community by reporting a lost or found item.</p>
  <form method="post" enctype="multipart/form-data" class="form" id="report">
    <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="type" id="type" value="{{ type }}">
    <div class="seg"><button type="button" data-type="lost">🔍 I Lost Something</button><button type="button" data-type="found">🙌 I Found Something</button></div>
    <label>Title *<input name="title" required maxlength="100" placeholder="e.g., Blue AirPods Pro case" value="{{ form.get('title', '') }}"></label>
    <label>Description<textarea name="description" rows="4" maxlength="600" placeholder="Describe the item in detail…">{{ form.get('description', '') }}</textarea></label>
    <div class="two">
      <label>Category *<select name="category" required><option value="">Select category</option>{% for c in CATEGORIES %}<option {{ 'selected' if form.get('category') == c }}>{{ c }}</option>{% endfor %}</select></label>
      <label>Location *<select name="location" required><option value="">Where?</option>{% for l in LOCATIONS %}<option {{ 'selected' if form.get('location') == l }}>{{ l }}</option>{% endfor %}</select></label>
    </div>
    <label><span id="dateLbl">Date lost</span><input type="date" name="date" max="{{ today }}" value="{{ form.get('date', today) }}"></label>
    <label id="secretWrap" hidden>Hidden detail for verification<input name="secret" maxlength="200" placeholder="Something only the owner would know"><small class="muted">Never shown publicly. Used to score claims.</small></label>
    <label>Photos of the item (max 5, helps others recognise it)<input type="file" name="photos" id="photos" accept="image/*" multiple></label>
    <div id="previews" class="previews"></div>
    <button class="btn primary wide" id="submitBtn">Post Lost Item</button>
  </form>
</section>
{% endblock %}
"""


# =====================================================================
#  CSS
# =====================================================================
CSS = r""":root{--bg:#f4f8f6;--surf:#fff;--ink:#16181f;--mute:#6b7280;--line:#e5e7eb;--pri:#4f46e5;--priT:#fff;--lost:#dc2626;--lostB:#fee2e2;--found:#16a34a;--foundB:#dcfce7;--warn:#b45309;--warnB:#fef3c7}
[data-theme=dark]{--bg:#0e1220;--surf:#171c2e;--ink:#eef1f8;--mute:#9aa4bc;--line:#2a3350;--pri:#818cf8;--priT:#0e1220;--lostB:#3b1d1d;--foundB:#10331f;--warnB:#3b2a0c;--warn:#fbbf24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--pri);text-decoration:none}
h1,h2,h3{margin:.4em 0;line-height:1.2}h1{font-size:28px}h2{font-size:20px;margin-top:28px}
.muted{color:var(--mute);font-size:14px}
.top{display:flex;justify-content:space-between;align-items:center;padding:10px 24px;background:var(--surf);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap;gap:8px}
.top nav{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.top nav a:not(.btn){color:var(--mute);font-size:14px}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--ink)}
.logo{background:var(--pri);color:var(--priT);width:30px;height:30px;border-radius:9px;display:grid;place-items:center}
.icon{background:none;border:0;font-size:18px;cursor:pointer;position:relative}
.dot{position:absolute;top:-6px;right:-8px;background:var(--lost);color:#fff;font:700 10px/1 sans-serif;padding:3px 5px;border-radius:99px}
.btn{display:inline-block;border:1px solid var(--line);background:var(--surf);color:var(--ink);padding:10px 16px;border-radius:10px;font:600 14px inherit;cursor:pointer;text-align:center}
.btn.primary{background:var(--pri);color:var(--priT);border-color:var(--pri)}.btn.dark{background:#111827;color:#fff}.btn.danger{color:var(--lost)}
.btn.sm{padding:6px 12px;font-size:13px}.btn.wide{width:100%;margin:12px 0}.btn.ghost-w{background:rgba(255,255,255,.18);color:#fff;border-color:rgba(255,255,255,.4)}
main{min-height:80vh}.wrap{max-width:1100px;margin:0 auto;padding:24px 20px}
.flash{max-width:1100px;margin:12px auto;padding:10px 14px;border-radius:10px;background:var(--foundB);color:var(--found)}.flash.error{background:var(--lostB);color:var(--lost)}
.hero{text-align:center;color:#fff;padding:90px 20px;background:linear-gradient(rgba(20,18,50,.55),rgba(20,18,50,.65)),url('__HERO__') center/cover,linear-gradient(135deg,#4f46e5,#0e8a7d)}
.hero h1{font-size:44px}.hero p{max-width:520px;margin:10px auto 22px;opacity:.9}
.search{display:flex;gap:8px;max-width:520px;margin:0 auto 14px}.search input{flex:1;border-radius:99px;border:0;padding:12px 18px}.search .btn{border-radius:99px}
.row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}.row.end{justify-content:flex-end}
.stats{display:grid;grid-template-columns:repeat(3,1fr);text-align:center;background:var(--surf);border-bottom:1px solid var(--line);padding:22px}
.stats.four{grid-template-columns:repeat(4,1fr);border:1px solid var(--line);border-radius:14px}
.stats b{display:block;font-size:30px}.stats span{font-size:11px;letter-spacing:.08em;color:var(--mute)}
.between{display:flex;justify-content:space-between;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-top:12px}
.card{background:var(--surf);border:1px solid var(--line);border-radius:14px;overflow:hidden;color:var(--ink)}
.thumb{height:150px;background:var(--bg);display:grid;place-items:center;font-size:44px;position:relative}.thumb img{width:100%;height:100%;object-fit:cover}
.tag{position:absolute;top:10px;left:10px;font-size:11px;padding:3px 9px;border-radius:99px}.tag.static{position:static;display:inline-block}
.tag.lost{background:var(--lostB);color:var(--lost)}.tag.found{background:var(--foundB);color:var(--found)}
.body{padding:12px 14px}.body h3{margin:0 0 4px;font-size:16px}
.chip{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:99px;padding:2px 10px;font-size:12px}
.chip.good{background:var(--foundB);color:var(--found)}.chip.warn{background:var(--warnB);color:var(--warn)}
.filters{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:8px;margin:12px 0}
input,select,textarea{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surf);color:var(--ink);font:inherit}
.panel{background:var(--surf);border:1px solid var(--line);border-radius:16px;padding:22px;margin:16px auto}.narrow{max-width:520px;margin-top:32px}
.form label{display:block;margin-top:14px;font-weight:500;font-size:14px}.form input,.form select,.form textarea{margin-top:6px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.seg{display:flex;gap:4px;background:var(--bg);padding:4px;border-radius:12px;margin-top:14px}
.seg button{flex:1;border:0;background:none;padding:9px;border-radius:9px;font:600 13px inherit;color:var(--mute);cursor:pointer}.seg button.on{background:var(--surf);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.15)}
.previews{display:flex;gap:8px;margin-top:8px}.previews img{width:64px;height:64px;object-fit:cover;border-radius:8px}
.detail{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:12px}
.gallery{display:grid;gap:8px}.gallery img{width:100%;border-radius:14px}.noimg{height:280px;display:grid;place-items:center;font-size:80px;background:var(--surf);border:1px solid var(--line);border-radius:14px}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px;background:var(--surf);border:1px solid var(--line);border-radius:12px;padding:14px}.facts dt{font-size:12px;color:var(--mute)}.facts dd{margin:0;font-weight:600}
.note{background:var(--warnB);color:var(--warn);padding:10px 14px;border-radius:10px}
.claim{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin:10px 0}
dialog{border:1px solid var(--line);border-radius:16px;background:var(--surf);color:var(--ink);max-width:420px;width:92%;padding:22px}dialog::backdrop{background:rgba(0,0,0,.5)}
.thumb:has(.auto-icon){background:linear-gradient(135deg,var(--bg),var(--line))}
.auto-icon{font-size:64px;filter:drop-shadow(0 4px 8px rgba(0,0,0,.18))}
@media(max-width:760px){.detail,.filters,.two{grid-template-columns:1fr}.hero h1{font-size:32px}.claim{flex-direction:column}.stats.four{grid-template-columns:1fr 1fr}}
"""

# =====================================================================
#  JAVASCRIPT
# =====================================================================
JS = r"""// Dark mode
document.getElementById('theme')?.addEventListener('click', () => {
  const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  localStorage.setItem('theme', t);
});

// Modals + character counter
document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => document.getElementById(b.dataset.open).showModal());
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());
document.querySelectorAll('[data-count]').forEach(t => {
  const out = document.querySelector(t.dataset.count);
  t.addEventListener('input', () => out.textContent = t.value.length + '/' + t.maxLength);
});

// Auto-submit browse filters
document.querySelectorAll('form[data-auto] select').forEach(s => s.onchange = () => s.form.submit());

// Report form: lost/found toggle
const typeInput = document.getElementById('type');
if (typeInput) {
  const setType = t => {
    typeInput.value = t;
    document.querySelectorAll('[data-type]').forEach(b => b.classList.toggle('on', b.dataset.type === t));
    document.getElementById('dateLbl').textContent = 'Date ' + t;
    document.getElementById('secretWrap').hidden = t !== 'found';
    document.getElementById('submitBtn').textContent = 'Post ' + (t === 'found' ? 'Found' : 'Lost') + ' Item';
  };
  document.querySelectorAll('[data-type]').forEach(b => b.onclick = () => setType(b.dataset.type));
  setType(typeInput.value);

  // Photo preview, max 5
  const photos = document.getElementById('photos'), prev = document.getElementById('previews');
  photos.onchange = () => {
    if (photos.files.length > 5) { alert('You can upload up to 5 photos.'); photos.value = ''; prev.innerHTML = ''; return; }
    prev.innerHTML = '';
    [...photos.files].forEach(f => { const i = document.createElement('img'); i.src = URL.createObjectURL(f); prev.appendChild(i); });
  };
}
"""


TEMPLATES["verify.html"] = r"""{% extends "base.html" %}
{% block title %}Verify email · CampusFind{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Check your email</h1>
  <p class="muted">We sent a 6-digit code to <b>{{ email }}</b>. It expires in 10 minutes.</p>
  {% if dev %}<p class="note">Email sending is not set up, so the code is printed in the server terminal.</p>{% endif %}
  <form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="email" value="{{ email }}">
    <label>Verification code<input name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required autocomplete="one-time-code" autofocus></label>
    <button class="btn primary wide">Verify and continue</button></form>
  <form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="email" value="{{ email }}"><input type="hidden" name="resend" value="1">
    <button class="btn wide">Resend code</button></form>
  <p class="muted">Wrong email? <a href="{{ url_for('register') }}">Start over</a></p>
</section>
{% endblock %}
"""

TEMPLATES["forgot.html"] = r"""{% extends "base.html" %}
{% block title %}Forgot password · CampusFind{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Forgot your password?</h1>
  <p class="muted">Enter your email and we will send a reset code.</p>
  <form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Email<input type="email" name="email" required></label>
    <button class="btn primary wide">Send reset code</button></form>
  <p class="muted"><a href="{{ url_for('login') }}">Back to sign in</a></p>
</section>
{% endblock %}
"""

TEMPLATES["reset.html"] = r"""{% extends "base.html" %}
{% block title %}Reset password · CampusFind{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>Set a new password</h1>
  {% if dev %}<p class="note">Email sending is not set up, so the code is printed in the server terminal.</p>{% endif %}
  <form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Email<input type="email" name="email" required value="{{ email }}"></label>
    <label>Reset code<input name="code" inputmode="numeric" maxlength="6" required autocomplete="one-time-code"></label>
    <label>New password<input type="password" name="password" required minlength="6"></label>
    <button class="btn primary wide">Update password</button></form>
</section>
{% endblock %}
"""


app.jinja_loader = DictLoader(TEMPLATES)
init_db()

if __name__ == "__main__":
    port = int(env("PORT", "5000"))
    print(f"\nCampusFind running at {BASE_URL if PRODUCTION else 'http://127.0.0.1:%d' % port}   (Ctrl+C to stop)")
    print("Email:", f"REAL via {SMTP['host']}" if SMTP["host"] else "DEV MODE - verification codes are printed in this terminal (set SMTP_* in .env for real email)")
    if SEED_DEMO:
        print("Demo logins: admin@poornima.edu.in / admin123  |  demo@poornima.edu.in / demo123")
    if not PRODUCTION:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host=env("HOST", "127.0.0.1"), port=port, debug=False)
