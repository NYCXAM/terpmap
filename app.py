import json
import os
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "umd_map.sqlite3")
UMD_CENTER = {"lat": 38.9869, "lng": -76.9426}

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(provider, provider_id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    db.commit()


@app.before_request
def ensure_schema():
    init_db()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def upsert_user(provider, provider_id, email, name):
    now = int(time.time())
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE provider = ? AND provider_id = ?",
        (provider, provider_id),
    ).fetchone()
    if user:
        return user

    cursor = db.execute(
        """
        INSERT INTO users (provider, provider_id, email, name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (provider, provider_id, email, name or email, now),
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("map_view"))
    return redirect(url_for("login"))


@app.route("/login")
def login():
    return render_template(
        "login.html",
        google_enabled=bool(app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]),
    )


@app.route("/auth/google")
def google_auth():
    if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
        flash("Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        return redirect(url_for("login"))

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {
        "client_id": app.config["GOOGLE_CLIENT_ID"],
        "redirect_uri": url_for("google_callback", _external=True),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "hd": "umd.edu",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400, "Invalid OAuth state")
    if "error" in request.args:
        flash(request.args["error"])
        return redirect(url_for("login"))

    code = request.args.get("code")
    token_payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": app.config["GOOGLE_CLIENT_ID"],
            "client_secret": app.config["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": url_for("google_callback", _external=True),
            "grant_type": "authorization_code",
        }
    ).encode()
    token_request = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=token_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(token_request, timeout=10) as response:
        tokens = json.loads(response.read().decode())

    user_request = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    with urllib.request.urlopen(user_request, timeout=10) as response:
        profile = json.loads(response.read().decode())

    if not profile.get("email_verified"):
        flash("Google account email must be verified.")
        return redirect(url_for("login"))

    user = upsert_user("google", profile["sub"], profile["email"], profile.get("name", profile["email"]))
    session["user_id"] = user["id"]
    return redirect(url_for("map_view"))


@app.route("/auth/umd/request", methods=["POST"])
def umd_request_code():
    email = request.form.get("email", "").strip().lower()
    if not email.endswith("@umd.edu"):
        flash("Use a valid @umd.edu email address.")
        return redirect(url_for("login"))

    code = f"{secrets.randbelow(1_000_000):06d}"
    session["umd_email"] = email
    session["umd_code"] = code
    session["umd_code_expires"] = int(time.time()) + 600
    print(f"[UMD Map] Verification code for {email}: {code}", flush=True)
    flash("Verification code generated. Check the Flask server console.")
    return redirect(url_for("login"))


@app.route("/auth/umd/verify", methods=["POST"])
def umd_verify_code():
    email = request.form.get("email", "").strip().lower()
    code = request.form.get("code", "").strip()
    if (
        email != session.get("umd_email")
        or code != session.get("umd_code")
        or int(time.time()) > session.get("umd_code_expires", 0)
    ):
        flash("Invalid or expired UMD verification code.")
        return redirect(url_for("login"))

    user = upsert_user("umd", email, email, email.split("@")[0])
    session.clear()
    session["user_id"] = user["id"]
    return redirect(url_for("map_view"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/map")
@login_required
def map_view():
    return render_template("map.html", user=current_user(), center=UMD_CENTER)


@app.route("/api/reports", methods=["GET"])
@login_required
def list_reports():
    rows = get_db().execute(
        """
        SELECT reports.*, users.name AS author_name
        FROM reports
        JOIN users ON users.id = reports.user_id
        ORDER BY reports.created_at DESC
        LIMIT 250
        """
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/reports", methods=["POST"])
@login_required
def create_report():
    payload = request.get_json(force=True)
    required = ["category", "severity", "title", "description", "lat", "lng"]
    if any(payload.get(field) in (None, "") for field in required):
        abort(400, "Missing required report fields")

    lat = float(payload["lat"])
    lng = float(payload["lng"])
    if not (38.94 <= lat <= 39.04 and -77.02 <= lng <= -76.88):
        abort(400, "Pin must be near the UMD College Park campus")

    user = current_user()
    cursor = get_db().execute(
        """
        INSERT INTO reports (user_id, category, severity, title, description, lat, lng, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            payload["category"][:40],
            payload["severity"][:20],
            payload["title"][:80],
            payload["description"][:600],
            lat,
            lng,
            int(time.time()),
        ),
    )
    get_db().commit()
    return jsonify({"id": cursor.lastrowid}), 201


if __name__ == "__main__":
    app.run(debug=True)
