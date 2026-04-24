import base64
import binascii
import json
import math
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps
from datetime import datetime, timedelta

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
REPORT_RETENTION_SECONDS = 30 * 24 * 60 * 60
FILTER_WINDOWS = {
    "today": "today",
    "week": "this_week",
    "two_weeks": "two_weeks",
    "month": "month",
}
SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2}
MAX_IMAGE_BYTES = 1_500_000
CATEGORIES = [
    "Traffic accident",
    "Road closure",
    "Safety alert",
    "Campus event",
    "Construction",
    "Other",
]
LOCATION_REFERENCES = [
    {"name": "Lot 1", "lat": 38.9892, "lng": -76.9451, "kind": "parking lot"},
    {"name": "Lot 3", "lat": 38.9915, "lng": -76.9443, "kind": "parking lot"},
    {"name": "Lot 6", "lat": 38.9938, "lng": -76.9392, "kind": "parking lot"},
    {"name": "Lot 9", "lat": 38.9902, "lng": -76.9514, "kind": "parking lot"},
    {"name": "Lot 11B", "lat": 38.9853, "lng": -76.9507, "kind": "parking lot"},
    {"name": "Lot Z", "lat": 38.9799, "lng": -76.9418, "kind": "parking lot"},
    {"name": "Stamp Student Union", "lat": 38.9881, "lng": -76.9447, "kind": "building"},
    {"name": "McKeldin Library", "lat": 38.9859, "lng": -76.9451, "kind": "building"},
    {"name": "Eppley Recreation Center", "lat": 38.9936, "lng": -76.9454, "kind": "building"},
    {"name": "Xfinity Center", "lat": 38.9958, "lng": -76.9412, "kind": "building"},
    {"name": "Iribe Center", "lat": 38.9891, "lng": -76.9365, "kind": "building"},
    {"name": "Brendan Iribe Center Drive", "lat": 38.9884, "lng": -76.9376, "kind": "road"},
    {"name": "Campus Drive", "lat": 38.9873, "lng": -76.9426, "kind": "road"},
    {"name": "Regents Drive", "lat": 38.9906, "lng": -76.9449, "kind": "road"},
    {"name": "Stadium Drive", "lat": 38.9929, "lng": -76.9476, "kind": "road"},
    {"name": "Baltimore Avenue", "lat": 38.9921, "lng": -76.9332, "kind": "road"},
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def load_dotenv_file():
    dotenv_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(dotenv_path):
        return

    with open(dotenv_path, "r", encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if value and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]

            os.environ.setdefault(key, value)


load_dotenv_file()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET", "")
app.config["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")
app.config["OPENAI_MODEL"] = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_column(table_name, column_name, ddl_fragment):
    db = get_db()
    existing_columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in existing_columns:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl_fragment}")


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

        CREATE TABLE IF NOT EXISTS report_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            value INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(report_id, user_id),
            FOREIGN KEY(report_id) REFERENCES reports(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_report_votes_report_id ON report_votes (report_id);
        """
    )
    ensure_column("reports", "image_data", "image_data TEXT NOT NULL DEFAULT ''")
    ensure_column("reports", "ai_summary", "ai_summary TEXT NOT NULL DEFAULT ''")
    ensure_column("reports", "summary_updated_at", "summary_updated_at INTEGER")
    ensure_column("reports", "merged_into", "merged_into INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS idx_reports_merged_into ON reports (merged_into)")
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


def validate_image_data(image_data):
    if not image_data:
        return ""
    if not image_data.startswith("data:image/") or ";base64," not in image_data:
        abort(400, "Attached image must be a valid image file.")

    _header, encoded = image_data.split(",", 1)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        abort(400, "Attached image could not be decoded.")

    if len(raw) > MAX_IMAGE_BYTES:
        abort(400, "Attached image must be smaller than 1.5MB.")
    return image_data


def fetch_reports_with_authors():
    rows = get_db().execute(
        """
        SELECT reports.*, users.name AS author_name, users.email AS author_email
        FROM reports
        JOIN users ON users.id = reports.user_id
        ORDER BY reports.created_at DESC
        """
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def resolve_root_id(report_id, reports_by_id):
    current_id = report_id
    seen = set()

    while True:
        report = reports_by_id.get(current_id)
        if report is None:
            return report_id

        parent_id = report.get("merged_into")
        if not parent_id or parent_id == current_id or parent_id in seen or parent_id not in reports_by_id:
            return current_id

        seen.add(current_id)
        current_id = parent_id


def build_report_groups(reports_by_id):
    groups = {}
    roots = {}

    for report_id in reports_by_id:
        root_id = resolve_root_id(report_id, reports_by_id)
        roots[report_id] = root_id
        groups.setdefault(root_id, []).append(report_id)

    return roots, groups


def cleanup_expired_reports():
    db = get_db()
    reports_by_id = {
        row["id"]: dict(row)
        for row in db.execute("SELECT id, created_at, merged_into FROM reports").fetchall()
    }
    if not reports_by_id:
        return

    _roots, groups = build_report_groups(reports_by_id)
    cutoff = int(time.time()) - REPORT_RETENTION_SECONDS
    expired_ids = []

    for report_ids in groups.values():
        last_activity = max(reports_by_id[report_id]["created_at"] for report_id in report_ids)
        if last_activity < cutoff:
            expired_ids.extend(report_ids)

    if not expired_ids:
        return

    placeholders = ",".join("?" for _ in expired_ids)
    db.execute(f"DELETE FROM report_votes WHERE report_id IN ({placeholders})", expired_ids)
    db.execute(f"DELETE FROM reports WHERE id IN ({placeholders})", expired_ids)
    db.commit()


def vote_summary_for_user(user_id):
    db = get_db()
    totals = {
        row["report_id"]: row["score"]
        for row in db.execute(
            "SELECT report_id, COALESCE(SUM(value), 0) AS score FROM report_votes GROUP BY report_id"
        ).fetchall()
    }
    user_votes = {
        row["report_id"]: row["value"]
        for row in db.execute(
            "SELECT report_id, value FROM report_votes WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    }
    return totals, user_votes


def cutoff_for_window(window_key):
    now = datetime.now()
    if window_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window_key == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif window_key == "two_weeks":
        start = now - timedelta(days=14)
    else:
        start = now - timedelta(days=30)
    return int(start.timestamp())


def dominant_severity(members):
    return max(members, key=lambda report: SEVERITY_RANK.get(report["severity"], 0))["severity"]


def distance_meters(lat_a, lng_a, lat_b, lng_b):
    lat_scale = 111_320
    lng_scale = math.cos(math.radians((lat_a + lat_b) / 2)) * 111_320
    delta_lat = (lat_a - lat_b) * lat_scale
    delta_lng = (lng_a - lng_b) * lng_scale
    return math.hypot(delta_lat, delta_lng)


def describe_event_location(lat, lng):
    ranked = sorted(
        (
            {
                "name": item["name"],
                "kind": item["kind"],
                "distance": distance_meters(lat, lng, item["lat"], item["lng"]),
            }
            for item in LOCATION_REFERENCES
        ),
        key=lambda item: item["distance"],
    )
    if not ranked:
        return "on campus"

    closest = ranked[0]
    nearby_road = next((item for item in ranked if item["kind"] == "road"), None)

    if closest["distance"] <= 90:
        base = f"at {closest['name']}"
    elif closest["distance"] <= 220:
        base = f"near {closest['name']}"
    else:
        base = "on campus"

    if nearby_road and nearby_road["name"] != closest["name"] and nearby_road["distance"] <= 180:
        connector = "on" if base == "on campus" else "off"
        return f"{base} {connector} {nearby_road['name']}"
    return base


def summarize_members_text(members):
    lines = []
    for member in sorted(members, key=lambda report: report["created_at"], reverse=True):
        lines.append(
            "\n".join(
                [
                    f"Title: {member['title']}",
                    f"Category: {member['category']}",
                    f"Severity: {member['severity']}",
                    f"Reported by: {member['author_name']}",
                    f"Time: {member['created_at']}",
                    f"Details: {member['description']}",
                ]
            )
        )
    return "\n\n".join(lines)


def summarize_campus_events_text(events):
    lines = []
    for event in sorted(events, key=lambda item: item["last_activity_at"], reverse=True):
        lines.append(
            "\n".join(
                [
                    f"Title: {event['title']}",
                    f"Location: {event['location_label']}",
                    f"Category: {event['category']}",
                    f"Severity: {event['severity']}",
                    f"Community score: {event['score']}",
                    f"Reports merged: {event['merged_count'] + 1}",
                    f"Last activity: {event['last_activity_at']}",
                    f"Details: {event['description']}",
                ]
            )
        )
    return "\n\n".join(lines)


def extract_response_text(response_payload):
    text_fragments = []
    for output_item in response_payload.get("output", []):
        for content_item in output_item.get("content", []):
            if content_item.get("type") == "output_text" and content_item.get("text"):
                text_fragments.append(content_item["text"])
    return "\n".join(fragment.strip() for fragment in text_fragments if fragment.strip()).strip()


def generate_ai_summary(members, user):
    if not app.config["OPENAI_API_KEY"]:
        abort(503, "AI summarizer is unavailable until OPENAI_API_KEY is configured.")

    prompt = (
        "Summarize this University of Maryland campus event in two concise sentences. "
        "Mention the likely situation, urgency, and any uncertainty without inventing details.\n\n"
        f"{summarize_members_text(members)}"
    )
    request_body = {
        "model": app.config["OPENAI_MODEL"],
        "instructions": (
            "You summarize student-reported campus events for a shared map. "
            "Be factual, concise, and cautious about uncertainty."
        ),
        "input": prompt,
        "max_output_tokens": 140,
        "temperature": 0.2,
        "store": False,
        "text": {"format": {"type": "text"}},
        "safety_identifier": f"umd-map-user-{user['id']}",
    }
    http_request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {app.config['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")
        abort(502, f"AI summarizer request failed: {detail[:200] or error.reason}")
    except urllib.error.URLError:
        abort(502, "AI summarizer could not reach the OpenAI API.")

    summary = extract_response_text(payload)
    if not summary:
        abort(502, "AI summarizer returned an empty summary.")
    return summary[:600]


def generate_campus_summary(events, user, window_key, category_filter):
    if not app.config["OPENAI_API_KEY"]:
        abort(503, "AI summarizer is unavailable until OPENAI_API_KEY is configured.")
    if not events:
        return "No visible events match the current filters, so there is nothing to summarize yet."

    category_label = category_filter if category_filter else "All categories"
    prompt = (
        "You are summarizing the current campus situation at the University of Maryland based only on the visible event feed. "
        "Write 3 short bullet points followed by a single sentence overall takeaway. "
        "Only describe patterns supported by the reports below. Mention named locations when they are provided and helpful. "
        "Mention uncertainty when reports are sparse.\n\n"
        f"Active filter window: {window_key}\n"
        f"Active category filter: {category_label}\n"
        f"Visible events count: {len(events)}\n\n"
        f"{summarize_campus_events_text(events)}"
    )
    request_body = {
        "model": app.config["OPENAI_MODEL"],
        "instructions": (
            "You summarize the current campus-wide situation for students scanning a live map. "
            "Be concise, grounded in the provided events, and avoid invented specifics."
        ),
        "input": prompt,
        "max_output_tokens": 220,
        "temperature": 0.2,
        "store": False,
        "text": {"format": {"type": "text"}},
        "safety_identifier": f"umd-map-campus-{user['id']}",
    }
    http_request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {app.config['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")
        abort(502, f"AI summarizer request failed: {detail[:200] or error.reason}")
    except urllib.error.URLError:
        abort(502, "AI summarizer could not reach the OpenAI API.")

    summary = extract_response_text(payload)
    if not summary:
        abort(502, "AI summarizer returned an empty summary.")
    return summary[:1200]


def consolidate_votes(root_id, group_ids):
    db = get_db()
    if not group_ids:
        return

    placeholders = ",".join("?" for _ in group_ids)
    rows = db.execute(
        f"""
        SELECT id, report_id, user_id, value, created_at, updated_at
        FROM report_votes
        WHERE report_id IN ({placeholders})
        ORDER BY updated_at DESC, created_at DESC, id DESC
        """,
        group_ids,
    ).fetchall()

    preferred_votes = {}
    for row in rows:
        current = preferred_votes.get(row["user_id"])
        if current is None:
            preferred_votes[row["user_id"]] = dict(row)
            continue
        if current["report_id"] != root_id and row["report_id"] == root_id:
            preferred_votes[row["user_id"]] = dict(row)

    db.execute(f"DELETE FROM report_votes WHERE report_id IN ({placeholders})", group_ids)
    now = int(time.time())
    for vote in preferred_votes.values():
        db.execute(
            """
            INSERT INTO report_votes (report_id, user_id, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                root_id,
                vote["user_id"],
                vote["value"],
                vote["created_at"] or now,
                now,
            ),
        )


def build_event_payload(root_id, group_ids, reports_by_id, vote_totals, user_votes):
    members = [reports_by_id[report_id] for report_id in group_ids]
    root = reports_by_id[root_id]
    members.sort(key=lambda report: report["created_at"], reverse=True)

    image_data = root["image_data"] or next((member["image_data"] for member in members if member["image_data"]), "")
    user_vote_total = sum(user_votes.get(report_id, 0) for report_id in group_ids)
    categories = {member["category"] for member in members}
    titles = [member["title"] for member in members if member["id"] != root_id]
    event_lat = round(sum(member["lat"] for member in members) / len(members), 6)
    event_lng = round(sum(member["lng"] for member in members) / len(members), 6)
    location_label = describe_event_location(event_lat, event_lng)

    return {
        "id": root_id,
        "title": root["title"],
        "description": root["description"],
        "ai_summary": root["ai_summary"],
        "category": root["category"] if len(categories) == 1 else "Multiple reports",
        "severity": dominant_severity(members),
        "lat": event_lat,
        "lng": event_lng,
        "location_label": location_label,
        "created_at": root["created_at"],
        "last_activity_at": max(member["created_at"] for member in members),
        "author_name": root["author_name"],
        "image_data": image_data,
        "score": sum(vote_totals.get(report_id, 0) for report_id in group_ids),
        "user_vote": 1 if user_vote_total > 0 else -1 if user_vote_total < 0 else 0,
        "merged_count": max(len(members) - 1, 0),
        "merged_titles": titles,
        "reports": [
            {
                "id": member["id"],
                "title": member["title"],
                "description": member["description"],
                "category": member["category"],
                "severity": member["severity"],
                "location_label": describe_event_location(member["lat"], member["lng"]),
                "created_at": member["created_at"],
                "author_name": member["author_name"],
            }
            for member in members
        ],
    }


def matches_search(event_payload, search_term):
    if not search_term:
        return True

    haystack = " ".join(
        [
            event_payload["title"],
            event_payload["description"],
            event_payload["ai_summary"],
            event_payload["category"],
            event_payload["severity"],
            event_payload["location_label"],
            event_payload["author_name"],
            " ".join(event_payload["merged_titles"]),
            " ".join(report["description"] for report in event_payload["reports"]),
            " ".join(report["location_label"] for report in event_payload["reports"]),
        ]
    ).lower()
    return search_term in haystack


def apply_event_filters(events, window_key, category_filter, search_term):
    cutoff = cutoff_for_window(window_key)
    filtered = []
    for event_payload in events:
        if event_payload["last_activity_at"] < cutoff:
            continue
        if category_filter and event_payload["category"] != category_filter:
            continue
        if not matches_search(event_payload, search_term):
            continue
        filtered.append(event_payload)
    filtered.sort(key=lambda item: item["last_activity_at"], reverse=True)
    return filtered[:250]


def load_filtered_events(user_id, window_key, category_filter, search_term):
    reports_by_id = fetch_reports_with_authors()
    if not reports_by_id:
        return []

    _roots, groups = build_report_groups(reports_by_id)
    vote_totals, user_votes = vote_summary_for_user(user_id)
    events = [
        build_event_payload(root_id, group_ids, reports_by_id, vote_totals, user_votes)
        for root_id, group_ids in groups.items()
    ]
    return apply_event_filters(events, window_key, category_filter, search_term)


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
    return render_template(
        "map.html",
        user=current_user(),
        center=UMD_CENTER,
        ai_enabled=bool(app.config["OPENAI_API_KEY"]),
        categories=CATEGORIES,
    )


@app.route("/api/reports", methods=["GET"])
@login_required
def list_reports():
    cleanup_expired_reports()
    window_key = request.args.get("window", "week")
    if window_key not in FILTER_WINDOWS:
        abort(400, "Invalid filter window")

    category_filter = request.args.get("category", "").strip()
    if category_filter and category_filter not in CATEGORIES:
        abort(400, "Invalid category filter")

    search_term = request.args.get("search", "").strip().lower()
    events = load_filtered_events(current_user()["id"], window_key, category_filter, search_term)
    return jsonify(events)


@app.route("/api/reports", methods=["POST"])
@login_required
def create_report():
    cleanup_expired_reports()
    payload = request.get_json(force=True) or {}
    required = ["category", "severity", "title", "description", "lat", "lng"]
    if any(payload.get(field) in (None, "") for field in required):
        abort(400, "Missing required report fields")

    try:
        lat = float(payload["lat"])
        lng = float(payload["lng"])
    except (TypeError, ValueError):
        abort(400, "Latitude and longitude must be valid numbers.")
    if not (38.94 <= lat <= 39.04 and -77.02 <= lng <= -76.88):
        abort(400, "Pin must be near the UMD College Park campus")

    image_data = validate_image_data(payload.get("image_data", ""))
    user = current_user()
    now = int(time.time())
    cursor = get_db().execute(
        """
        INSERT INTO reports (
            user_id, category, severity, title, description, lat, lng, created_at, image_data, ai_summary, summary_updated_at, merged_into
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            payload["category"][:40],
            payload["severity"][:20],
            payload["title"][:80],
            payload["description"][:600],
            lat,
            lng,
            now,
            image_data,
            "",
            None,
            None,
        ),
    )
    get_db().commit()
    return jsonify({"id": cursor.lastrowid}), 201


@app.route("/api/reports/<int:report_id>/vote", methods=["POST"])
@login_required
def vote_report(report_id):
    cleanup_expired_reports()
    payload = request.get_json(force=True) or {}
    try:
        value = int(payload.get("value"))
    except (TypeError, ValueError):
        abort(400, "Vote must be -1, 0, or 1.")

    if value not in (-1, 0, 1):
        abort(400, "Vote must be -1, 0, or 1.")

    reports_by_id = fetch_reports_with_authors()
    if report_id not in reports_by_id:
        abort(404, "Report not found.")

    root_id = resolve_root_id(report_id, reports_by_id)
    user = current_user()
    now = int(time.time())

    if value == 0:
        get_db().execute(
            "DELETE FROM report_votes WHERE report_id = ? AND user_id = ?",
            (root_id, user["id"]),
        )
    else:
        get_db().execute(
            """
            INSERT INTO report_votes (report_id, user_id, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(report_id, user_id)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (root_id, user["id"], value, now, now),
        )
    get_db().commit()
    return jsonify({"ok": True, "report_id": root_id})


@app.route("/api/reports/merge", methods=["POST"])
@login_required
def merge_reports():
    cleanup_expired_reports()
    payload = request.get_json(force=True) or {}

    try:
        target_id = int(payload.get("target_id"))
        source_id = int(payload.get("source_id"))
    except (TypeError, ValueError):
        abort(400, "Merge requires two valid event ids.")

    if target_id == source_id:
        abort(400, "Choose two different events to merge.")

    reports_by_id = fetch_reports_with_authors()
    if target_id not in reports_by_id or source_id not in reports_by_id:
        abort(404, "One of the selected events no longer exists.")

    _roots, groups = build_report_groups(reports_by_id)
    target_root = resolve_root_id(target_id, reports_by_id)
    source_root = resolve_root_id(source_id, reports_by_id)

    if target_root == source_root:
        abort(400, "Those events are already merged.")

    target_group_ids = groups.get(target_root, [target_root])
    source_group_ids = groups.get(source_root, [source_root])

    db = get_db()
    db.execute(
        f"UPDATE reports SET merged_into = ? WHERE id IN ({','.join('?' for _ in source_group_ids)})",
        [target_root, *source_group_ids],
    )

    source_members = [reports_by_id[report_id] for report_id in source_group_ids]
    target_report = reports_by_id[target_root]
    fallback_image = target_report["image_data"] or next(
        (member["image_data"] for member in source_members if member["image_data"]),
        "",
    )
    fallback_summary = target_report["ai_summary"] or reports_by_id[source_root]["ai_summary"]
    db.execute(
        """
        UPDATE reports
        SET image_data = ?, ai_summary = ?, summary_updated_at = ?
        WHERE id = ?
        """,
        (
            fallback_image,
            fallback_summary,
            target_report["summary_updated_at"] or reports_by_id[source_root]["summary_updated_at"],
            target_root,
        ),
    )

    consolidate_votes(target_root, target_group_ids + source_group_ids)
    db.commit()
    return jsonify({"ok": True, "target_id": target_root})


@app.route("/api/summary", methods=["POST"])
@login_required
def summarize_campus():
    cleanup_expired_reports()
    payload = request.get_json(silent=True) or {}
    window_key = payload.get("window", "week")
    if window_key not in FILTER_WINDOWS:
        abort(400, "Invalid filter window")

    category_filter = str(payload.get("category", "")).strip()
    if category_filter and category_filter not in CATEGORIES:
        abort(400, "Invalid category filter")

    search_term = str(payload.get("search", "")).strip().lower()
    events = load_filtered_events(current_user()["id"], window_key, category_filter, search_term)
    summary = generate_campus_summary(events, current_user(), window_key, category_filter)
    return jsonify({"summary": summary, "count": len(events)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(debug=True, port=port)
