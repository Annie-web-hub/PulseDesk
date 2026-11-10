from __future__ import annotations

import csv
import io
import os
import re
import secrets
import smtplib
import sqlite3
from email.message import EmailMessage
from functools import wraps
from html import escape
from urllib.parse import urlparse
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DATABASE = Path(os.environ.get("PULSEDESK_DATABASE_PATH", INSTANCE_DIR / "pulsedesk.db"))

app = Flask(__name__)
secret_path = INSTANCE_DIR / ".secret"
if not secret_path.exists():
    secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = os.environ.get("PULSEDESK_SECRET_KEY", secret_path.read_text(encoding="utf-8").strip())
app.config.update(
    DATABASE=str(DATABASE),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PULSEDESK_SECURE_COOKIES", "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

STATUSES = ("Open", "In Progress", "Waiting", "Resolved")
PRIORITIES = ("Low", "Medium", "High", "Critical")
CATEGORIES = ("Hardware", "Software", "Access", "Network", "Email", "Other")
ASSET_STATUSES = ("In use", "In repair", "In stock", "Retired")
ROLES = ("Admin", "IT Agent", "Employee")
ESCALATION_TARGETS = {1: "Service Lead", 2: "IT Manager", 3: "Incident Manager"}
DEMO_ACCOUNTS = (
    ("admin@pulsedesk.demo", "Alex Morgan", "Admin", "AdminDemo!2026"),
    ("agent@pulsedesk.demo", "Jordan Lee", "IT Agent", "AgentDemo!2026"),
    ("employee@pulsedesk.demo", "Maya Patel", "Employee", "EmployeeDemo!2026"),
)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def load_user_and_check_csrf():
    token = session.get("_csrf_token")
    if not token:
        session["_csrf_token"] = secrets.token_urlsafe(32)
        token = session["_csrf_token"]
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        if not submitted or not secrets.compare_digest(token, submitted):
            abort(400, description="Your form session expired. Refresh the page and try again.")
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        user = get_db().execute(
            "SELECT id, email, full_name, role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user and user["active"]:
            g.user = user
        else:
            session.clear()
            session["_csrf_token"] = secrets.token_urlsafe(32)
    if request.endpoint not in ("login", "static") and g.user is None:
        return redirect(url_for("login", next=request.path))




def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(timespec="seconds")


def log_activity(db: sqlite3.Connection, action: str, detail: str, actor: str = "Alex Morgan") -> None:
    db.execute(
        "INSERT INTO activity (action, detail, actor, created_at) VALUES (?, ?, ?, ?)",
        (action, detail, actor, now_iso()),
    )


def smtp_is_configured() -> bool:
    return bool(os.environ.get("PULSEDESK_SMTP_HOST") and os.environ.get("PULSEDESK_SMTP_FROM"))


def deliver_email(db: sqlite3.Connection, recipient: str, subject: str, body: str) -> str:
    """Send email when SMTP is configured; otherwise retain an honest local preview."""
    if not recipient:
        return "skipped"
    status = "preview"
    error = ""
    sent_at = None
    if smtp_is_configured():
        try:
            message = EmailMessage()
            message["Subject"] = subject[:180]
            message["From"] = os.environ["PULSEDESK_SMTP_FROM"]
            message["To"] = recipient
            message["X-Mailer"] = "PulseDesk"
            message.set_content(body[:8000])
            host = os.environ["PULSEDESK_SMTP_HOST"]
            port = int(os.environ.get("PULSEDESK_SMTP_PORT", "587"))
            starttls = os.environ.get("PULSEDESK_SMTP_STARTTLS", "1") == "1"
            with smtplib.SMTP(host, port, timeout=5) as client:
                client.ehlo()
                if starttls:
                    client.starttls()
                    client.ehlo()
                username = os.environ.get("PULSEDESK_SMTP_USERNAME", "")
                password = os.environ.get("PULSEDESK_SMTP_PASSWORD", "")
                if username:
                    client.login(username, password)
                client.send_message(message)
            status = "sent"
            sent_at = now_iso()
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            status = "failed"
            error = str(exc)[:300]
    db.execute(
        """INSERT INTO email_outbox (recipient, subject, body, status, error, created_at, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (recipient[:200], subject[:180], body[:8000], status, error, now_iso(), sent_at),
    )
    return status


def create_notification(
    db: sqlite3.Connection,
    user_id: int,
    kind: str,
    title: str,
    message: str,
    href: str,
    email_subject: str | None = None,
) -> None:
    user = db.execute(
        "SELECT id, email FROM users WHERE id = ? AND active = 1", (user_id,)
    ).fetchone()
    if not user:
        return
    db.execute(
        """INSERT INTO notifications (user_id, kind, title, message, href, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, kind[:30], title[:160], message[:500], href[:300], now_iso()),
    )
    subject = email_subject or f"PulseDesk: {title}"
    body = f"{message}\\n\\nOpen PulseDesk to review this update: {href}"
    deliver_email(db, user["email"], subject, body)


def notify_roles(
    db: sqlite3.Connection,
    roles: tuple[str, ...],
    kind: str,
    title: str,
    message: str,
    href: str,
    exclude_user_id: int | None = None,
) -> None:
    placeholders = ",".join("?" for _ in roles)
    params: list[object] = list(roles)
    query = f"SELECT id FROM users WHERE active = 1 AND role IN ({placeholders})"
    if exclude_user_id is not None:
        query += " AND id != ?"
        params.append(exclude_user_id)
    for row in db.execute(query, params).fetchall():
        create_notification(db, row["id"], kind, title, message, href)


def notify_ticket_owner(
    db: sqlite3.Connection,
    ticket: sqlite3.Row,
    kind: str,
    title: str,
    message: str,
) -> None:
    owner_id = ticket["created_by"]
    if owner_id:
        create_notification(
            db, owner_id, kind, title, message, f"/tickets/{ticket['id']}"
        )


def recommend_priority(title: str, description: str, category: str) -> tuple[str, str]:
    text = f"{title} {description}".lower()
    if any(word in text for word in ("outage", "breach", "ransomware", "server down", "all users", "data loss")):
        return "Critical", "Potential service-wide or security incident detected."
    if any(word in text for word in ("cannot work", "production blocked", "locked out", "network down", "urgent")):
        return "High", "Work is blocked or a key service may be unavailable."
    if category in ("Access", "Network") or any(word in text for word in ("intermittent", "slow", "permission")):
        return "Medium", "A service or access issue needs investigation."
    return "Low", "No urgent impact signals detected. Confirm impact with the requester."


def init_db() -> None:
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin', 'IT Agent', 'Employee')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                requester TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT 'Operations',
                category TEXT NOT NULL DEFAULT 'Other',
                priority TEXT NOT NULL DEFAULT 'Medium',
                status TEXT NOT NULL DEFAULT 'Open',
                assignee TEXT NOT NULL DEFAULT 'Unassigned',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_by INTEGER REFERENCES users(id),
                escalation_level INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ticket_escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                level INTEGER NOT NULL,
                target_role TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor_id INTEGER REFERENCES users(id),
                actor_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'update',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                href TEXT NOT NULL DEFAULT '/',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'preview',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_tag TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                assigned_to TEXT NOT NULL DEFAULT 'Unassigned',
                department TEXT NOT NULL DEFAULT 'Operations',
                status TEXT NOT NULL DEFAULT 'In stock',
                warranty_until TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
            CREATE INDEX IF NOT EXISTS idx_tickets_due ON tickets(due_at);
            CREATE INDEX IF NOT EXISTS idx_escalations_ticket ON ticket_escalations(ticket_id, id);
            CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, id);
            CREATE INDEX IF NOT EXISTS idx_activity_id ON activity(id);
            CREATE INDEX IF NOT EXISTS idx_email_outbox_created ON email_outbox(created_at);
            """
        )
        ticket_columns = {row["name"] for row in db.execute("PRAGMA table_info(tickets)")}
        if "created_by" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN created_by INTEGER REFERENCES users(id)")
        if "escalation_level" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN escalation_level INTEGER NOT NULL DEFAULT 0")

        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for email, full_name, role, password in DEMO_ACCOUNTS:
                db.execute(
                    "INSERT INTO users (email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, full_name, generate_password_hash(password), role, now_iso()),
                )
        if db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0:
            seed_demo_data(db)
        if db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0:
            welcome = (
                ("admin@pulsedesk.demo", "Control room ready", "Your team workspace and notification centre are ready.", "/notifications"),
                ("agent@pulsedesk.demo", "Your incident feed is live", "New incident activity will appear in your workspace.", "/notifications"),
                ("employee@pulsedesk.demo", "Support desk connected", "You can now submit requests and follow updates from IT.", "/my-tickets"),
            )
            for email, title, message, href in welcome:
                user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                if user:
                    db.execute(
                        "INSERT INTO notifications (user_id, kind, title, message, href, created_at) VALUES (?, 'system', ?, ?, ?, ?)",
                        (user["id"], title, message, href, now_iso()),
                    )


def seed_demo_data(db: sqlite3.Connection) -> None:
    now = datetime.now().replace(microsecond=0)
    demo_tickets = [
        ("VPN connection drops during client calls", "The VPN disconnects several times an hour when connected from home. This is blocking client work.", "Maya Patel", "Sales", "Network", "High", "In Progress", "Jordan Lee", -2, 2),
        ("New starter needs laptop and access", "Prepare a laptop, email account and standard application access for the new analyst starting Monday.", "Oliver Grant", "People", "Hardware", "Medium", "Open", "Priya Shah", -1, 22),
        ("Shared finance drive unavailable", "The finance team cannot access the shared drive. Several users are affected and month-end work is blocked.", "Sophie Turner", "Finance", "Network", "Critical", "Open", "Jordan Lee", -1, -1),
        ("Adobe licence renewal", "The design team needs three additional Creative Cloud seats before the next campaign.", "Noah Williams", "Marketing", "Software", "Low", "Waiting", "Priya Shah", -4, 46),
        ("Multi-factor authentication reset", "A team member replaced their phone and cannot complete MFA verification.", "Ava Chen", "Operations", "Access", "Medium", "Resolved", "Alex Morgan", -5, -2),
        ("Printer queue stuck on floor 3", "Print jobs remain in the queue and do not reach the shared printer.", "Leo Martin", "Finance", "Hardware", "Low", "Open", "Unassigned", -1, 30),
        ("Suspicious sign-in notification", "A user received an unexpected sign-in alert from an unfamiliar location. Please investigate.", "Emma Wilson", "People", "Access", "High", "In Progress", "Alex Morgan", -1, 1),
        ("CRM page loading very slowly", "Customer records take more than 20 seconds to load during peak hours.", "Isabella Brown", "Sales", "Software", "Medium", "Open", "Jordan Lee", -3, 8),
        ("Meeting room display has no signal", "The HDMI display in meeting room Cedar is not detecting connected laptops.", "Ethan Davis", "Operations", "Hardware", "Low", "Resolved", "Priya Shah", -7, -1),
        ("Email delivery delayed for whole team", "Several departments report delayed incoming email. Please check the mail service status.", "Grace Kim", "Marketing", "Email", "High", "Waiting", "Alex Morgan", -2, 1),
        ("Request access to analytics workspace", "Please add read access to the quarterly reporting workspace.", "Lucas Reed", "Sales", "Access", "Low", "Resolved", "Jordan Lee", -8, -4),
        ("Database server down in staging", "The staging database server is down and the release validation pipeline cannot run.", "Mia Thompson", "Engineering", "Network", "Critical", "In Progress", "Jordan Lee", -1, -1),
    ]
    for title, desc, requester, dept, category, priority, status, assignee, created_days, due_hours in demo_tickets:
        created = now + timedelta(days=created_days)
        db.execute(
            """INSERT INTO tickets
            (title, description, requester, department, category, priority, status, assignee, created_at, updated_at, due_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, desc, requester, dept, category, priority, status, assignee,
             created.isoformat(timespec="seconds"), (created + timedelta(hours=3)).isoformat(timespec="seconds"),
             (now + timedelta(hours=due_hours)).isoformat(timespec="seconds")),
        )

    demo_assets = [
        ("AST-001", "MacBook Pro 14", "Laptop", "Maya Patel", "Sales", "In use", "2027-08-12"),
        ("AST-002", "Dell Latitude 7440", "Laptop", "Oliver Grant", "People", "In use", "2027-09-02"),
        ("AST-003", "ThinkPad X1 Carbon", "Laptop", "Jordan Lee", "IT", "In use", "2027-02-18"),
        ("AST-004", "iPhone 15", "Mobile", "Sophie Turner", "Finance", "In use", "2027-01-10"),
        ("AST-005", "Dell UltraSharp 27", "Monitor", "Unassigned", "Operations", "In stock", "2027-11-20"),
        ("AST-006", "HP LaserJet Pro", "Printer", "Finance team", "Finance", "In repair", "2026-12-01"),
        ("AST-007", "MacBook Air M3", "Laptop", "Noah Williams", "Marketing", "In use", "2028-01-15"),
        ("AST-008", "iPad Air", "Tablet", "Meeting room Cedar", "Operations", "In stock", "2027-05-05"),
    ]
    db.executemany(
        "INSERT INTO assets (asset_tag, name, asset_type, assigned_to, department, status, warranty_until) VALUES (?, ?, ?, ?, ?, ?, ?)",
        demo_assets,
    )

    articles = [
        ("Reset your multi-factor authentication", "Access", "Regain account access when replacing a phone or losing an authenticator.", "## Before you begin\nMake sure you can access your company email or contact the IT desk.\n\n## Steps\n1. Open the identity portal and choose **Security info**.\n2. Remove the old authenticator method if the device is no longer available.\n3. Add the new authenticator app and scan the QR code.\n4. Complete the test notification before closing the page.\n\n**Still locked out?** Raise an Access ticket and include your preferred callback method.", 18),
        ("Connect to the company VPN", "Network", "A quick checklist for connecting securely from outside the office.", "## Connection checklist\n1. Make sure your internet connection is stable.\n2. Open the approved VPN client.\n3. Sign in with your company account and approve the MFA request.\n4. Wait for the connected status before opening internal tools.\n\nIf the VPN repeatedly disconnects, note the time and network type in your ticket.", 32),
        ("Prepare a laptop for a new starter", "Hardware", "The standard device setup checklist for employee onboarding.", "## Setup checklist\n- [ ] Confirm the approved device and asset tag.\n- [ ] Install operating system updates.\n- [ ] Enrol the device in endpoint management.\n- [ ] Enable disk encryption and screen lock.\n- [ ] Install approved business applications.\n- [ ] Verify email, collaboration and VPN access.\n- [ ] Record the assigned user in the asset register.", 24),
        ("Report a suspicious sign-in", "Security", "What to do if you receive an unexpected account security alert.", "## Act promptly\n1. Do not approve an unexpected MFA prompt.\n2. Report the alert to IT or Security using the approved channel.\n3. Include the time, device and approximate location shown in the alert.\n4. Do not forward suspicious links or attachments to colleagues.\n\nThe security team will review sign-in logs and advise on next steps.", 41),
    ]
    for title, category, summary, content, views in articles:
        db.execute(
            "INSERT INTO articles (title, category, summary, content, updated_at, views) VALUES (?, ?, ?, ?, ?, ?)",
            (title, category, summary, content, now.isoformat(timespec="seconds"), views),
        )
    db.execute(
        "INSERT INTO activity (action, detail, actor, created_at) VALUES (?, ?, ?, ?)",
        ("Workspace created", "PulseDesk demo workspace is ready to use.", "System", now.isoformat(timespec="seconds")),
    )


@app.template_filter("article_html")
def article_html(value: str) -> str:
    """Render a small Markdown subset after escaping all source text."""
    output = []
    in_list = False
    for raw in str(value or "").splitlines():
        line = escape(raw.strip())
        if line.startswith("## "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("- ") or re.match(r"^[0-9]+[.] ", line):
            if not in_list:
                output.append("<ul>")
                in_list = True
            content = re.sub(r"^(-|[0-9]+[.]) ", "", line).replace("**", "")
            output.append(f"<li>{content}</li>")
        elif line:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{line.replace('**', '')}</p>")
        elif in_list:
            output.append("</ul>")
            in_list = False
    if in_list:
        output.append("</ul>")
    return "".join(output)


@app.template_filter("timeago")
def timeago(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        delta = datetime.now() - dt
        if delta.total_seconds() < 60:
            return "Just now"
        if delta.days:
            return f"{delta.days}d ago"
        if delta.seconds >= 3600:
            return f"{delta.seconds // 3600}h ago"
        return f"{max(1, delta.seconds // 60)}m ago"
    except (TypeError, ValueError):
        return value or "—"


def ticket_sla(ticket: sqlite3.Row) -> str:
    if ticket["status"] == "Resolved":
        return "resolved"
    try:
        return "breached" if datetime.fromisoformat(ticket["due_at"]) < datetime.now() else "on-track"
    except ValueError:
        return "on-track"


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") is not None:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:160]
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT id, email, full_name, role, password_hash, active FROM users WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
        if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
            flash("Those sign-in details were not recognised. Please try again.", "error")
            return render_template("login.html"), 401
        session.clear()
        session["user_id"] = user["id"]
        session["_csrf_token"] = secrets.token_urlsafe(32)
        next_path = request.form.get("next", "")
        parsed = urlparse(next_path)
        safe_next = next_path if not parsed.netloc and next_path.startswith("/") and not next_path.startswith("//") else ""
        flash(f"Welcome back, {user['full_name'].split()[0]}.", "success")
        return redirect(safe_next or (url_for("my_tickets") if user["role"] == "Employee" else url_for("dashboard")))
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/my-tickets", methods=["GET", "POST"])
@login_required
def my_tickets():
    db = get_db()
    if g.user["role"] != "Employee":
        return redirect(url_for("tickets_page"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Other")
        department = request.form.get("department", "Operations").strip()[:60] or "Operations"
        if not title:
            flash("Add a short title so the IT team knows what you need.", "error")
            return redirect(url_for("my_tickets"))
        if category not in CATEGORIES:
            category = "Other"
        priority, _reason = recommend_priority(title, description, category)
        due = datetime.now() + timedelta(hours={"Critical": 2, "High": 8, "Medium": 24, "Low": 72}[priority])
        cursor = db.execute(
            """INSERT INTO tickets
            (title, description, requester, department, category, priority, status, assignee,
             created_at, updated_at, due_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'Open', 'Unassigned', ?, ?, ?, ?)""",
            (title[:140], description[:2000], g.user["full_name"], department, category, priority,
             now_iso(), now_iso(), due.replace(microsecond=0).isoformat(timespec="seconds"), g.user["id"]),
        )
        log_activity(db, "Employee request created", f"#{cursor.lastrowid} ? {title[:100]}", g.user["full_name"])
        notify_roles(
            db, ("Admin", "IT Agent"), "ticket", f"New request #{cursor.lastrowid}",
            f"{g.user['full_name']} submitted: {title[:120]}",
            f"/tickets/{cursor.lastrowid}", exclude_user_id=g.user["id"],
        )
        db.commit()
        flash(f"Your request #{cursor.lastrowid} has been sent to IT.", "success")
        return redirect(url_for("my_tickets"))
    tickets = db.execute("SELECT * FROM tickets WHERE created_by = ? ORDER BY created_at DESC", (g.user["id"],)).fetchall()
    return render_template("my_tickets.html", active_page="my_tickets", tickets=tickets)


@app.route("/admin/users", methods=["GET", "POST"])
@roles_required("Admin")
def admin_users():
    db = get_db()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:160]
        full_name = request.form.get("full_name", "").strip()[:100]
        role = request.form.get("role", "")
        password = request.form.get("password", "")
        if not email or "@" not in email or not full_name or role not in ROLES or len(password) < 12:
            flash("Provide a valid email, name, role and password of at least 12 characters.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO users (email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, full_name, generate_password_hash(password), role, now_iso()),
                )
                log_activity(db, "User added", f"{full_name} ? {role}", g.user["full_name"])
                db.commit()
                flash(f"Account for {full_name} created.", "success")
            except sqlite3.IntegrityError:
                flash("That email address already has an account.", "error")
        return redirect(url_for("admin_users"))
    users = db.execute("SELECT id, email, full_name, role, active, created_at FROM users ORDER BY full_name").fetchall()
    return render_template("admin_users.html", active_page="users", users=users, roles=ROLES)


@app.context_processor
def inject_globals():
    db = get_db()
    open_count = db.execute("SELECT COUNT(*) FROM tickets WHERE status != 'Resolved'").fetchone()[0]
    user_id = g.user["id"] if g.get("user") is not None else None
    unread_count = 0
    latest_notifications = []
    if user_id:
        unread_count = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,)
        ).fetchone()[0]
        latest_notifications = db.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 4", (user_id,)
        ).fetchall()
    activity_cursor = db.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    return {
        "current_user": g.get("user"),
        "csrf_token": session.get("_csrf_token", ""),
        "open_count_global": open_count,
        "unread_notification_count": unread_count,
        "latest_notifications": latest_notifications,
        "activity_cursor": activity_cursor,
        "smtp_configured": smtp_is_configured(),
        "now_string": now_iso(),
        "current_year": datetime.now().year,
        "weekday_label": datetime.now().strftime("%A").upper(),
        "ticket_statuses": STATUSES,
        "ticket_priorities": PRIORITIES,
        "ticket_categories": CATEGORIES,
        "asset_statuses": ASSET_STATUSES,
    }


@app.route("/")
@login_required
def dashboard():
    if g.user["role"] == "Employee":
        return redirect(url_for("my_tickets"))
    db = get_db()
    tickets = db.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
    open_count = sum(t["status"] in ("Open", "In Progress", "Waiting") for t in tickets)
    breached = [t for t in tickets if ticket_sla(t) == "breached"]
    critical = sum(t["priority"] == "Critical" and t["status"] != "Resolved" for t in tickets)
    resolved = sum(t["status"] == "Resolved" for t in tickets)
    active = sum(t["status"] == "In Progress" for t in tickets)
    priorities = {p: sum(t["priority"] == p and t["status"] != "Resolved" for t in tickets) for p in PRIORITIES}
    recent = db.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 6").fetchall()
    activity = db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 6").fetchall()
    assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    return render_template(
        "dashboard.html", active_page="overview", total=len(tickets), open_count=open_count,
        breached=len(breached), critical=critical, resolved=resolved, active=active,
        priorities=priorities, recent=recent, activity=activity, asset_count=assets,
        sla_health=round(100 * (len(tickets) - len(breached)) / max(1, len(tickets))),
    )


@app.route("/tickets", methods=["GET", "POST"])
@roles_required("Admin", "IT Agent")
def tickets_page():
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        requester = request.form.get("requester", "").strip()
        department = request.form.get("department", "Operations").strip()[:60] or "Operations"
        category = request.form.get("category", "Other")
        assignee = request.form.get("assignee", "Unassigned").strip()[:80] or "Unassigned"
        if not title or not requester:
            flash("Add a title and requester before creating the ticket.", "error")
            return redirect(url_for("tickets_page"))
        if category not in CATEGORIES:
            category = "Other"
        priority, reason = recommend_priority(title, description, category)
        due = datetime.now() + timedelta(hours={"Critical": 2, "High": 8, "Medium": 24, "Low": 72}[priority])
        cursor = db.execute(
            """INSERT INTO tickets (title, description, requester, department, category, priority, status, assignee, created_at, updated_at, due_at)
            VALUES (?, ?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?)""",
            (title[:140], description[:2000], requester[:100], department, category, priority, assignee,
             now_iso(), now_iso(), due.replace(microsecond=0).isoformat(timespec="seconds")),
        )
        log_activity(db, "Ticket created", f"#{cursor.lastrowid} · {title[:100]} · {priority} priority", g.user["full_name"])
        notify_roles(
            db, ("Admin", "IT Agent"), "ticket", f"Incident #{cursor.lastrowid} created",
            f"{g.user['full_name']} logged: {title[:120]}",
            f"/tickets/{cursor.lastrowid}", exclude_user_id=g.user["id"],
        )
        db.commit()
        flash(f"Ticket #{cursor.lastrowid} created. Triage suggested {priority} priority.", "success")
        return redirect(url_for("ticket_detail", ticket_id=cursor.lastrowid))

    status = request.args.get("status", "")
    priority = request.args.get("priority", "")
    sla_filter = request.args.get("sla", "")
    q = request.args.get("q", "").strip()[:100]
    clauses, params = [], []
    if status in STATUSES:
        clauses.append("status = ?")
        params.append(status)
    if priority in PRIORITIES:
        clauses.append("priority = ?")
        params.append(priority)
    if sla_filter == "breached":
        clauses.append("status != 'Resolved' AND due_at < ?")
        params.append(now_iso())
    if q:
        clauses.append("(title LIKE ? OR requester LIKE ? OR department LIKE ? OR CAST(id AS TEXT) = ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", q.lstrip("#")])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    tickets = db.execute(f"SELECT * FROM tickets{where} ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END, created_at DESC", params).fetchall()
    return render_template("tickets.html", active_page="tickets", tickets=tickets, filters={"status": status, "priority": priority, "sla": sla_filter, "q": q})


@app.route("/tickets/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def ticket_detail(ticket_id: int):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        abort(404)
    if g.user["role"] == "Employee" and ticket["created_by"] != g.user["id"]:
        abort(404)
    if g.user["role"] == "Employee" and request.method == "POST":
        abort(403)
    if request.method == "POST":
        status = request.form.get("status", ticket["status"])
        priority = request.form.get("priority", ticket["priority"])
        assignee = request.form.get("assignee", ticket["assignee"]).strip()[:80]
        if status not in STATUSES or priority not in PRIORITIES:
            abort(400)
        db.execute(
            "UPDATE tickets SET status = ?, priority = ?, assignee = ?, updated_at = ? WHERE id = ?",
            (status, priority, assignee or "Unassigned", now_iso(), ticket_id),
        )
        log_activity(db, "Ticket updated", f"#{ticket_id} ? status: {status} ? priority: {priority}", g.user["full_name"])
        if status != ticket["status"] or priority != ticket["priority"]:
            notify_ticket_owner(
                db, ticket, "status",
                f"Request #{ticket_id} updated",
                f"Your request ?{ticket['title']}? is now {status} with {priority} priority.",
            )
        db.commit()
        flash(f"Ticket #{ticket_id} updated.", "success")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    suggested_priority, triage_reason = recommend_priority(ticket["title"], ticket["description"], ticket["category"])
    escalation_history = db.execute(
        "SELECT * FROM ticket_escalations WHERE ticket_id = ? ORDER BY id DESC", (ticket_id,)
    ).fetchall()
    return render_template("ticket_detail.html", active_page="tickets", ticket=ticket,
                           sla=ticket_sla(ticket), suggested_priority=suggested_priority, triage_reason=triage_reason,
                           escalation_history=escalation_history, escalation_targets=ESCALATION_TARGETS)


@app.post("/tickets/<int:ticket_id>/escalate")
@roles_required("Admin", "IT Agent")
def escalate_ticket(ticket_id: int):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        abort(404)
    if ticket["status"] == "Resolved":
        flash("Resolved incidents cannot be escalated.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    next_level = int(ticket["escalation_level"] or 0) + 1
    if next_level not in ESCALATION_TARGETS:
        flash("This incident has reached the highest escalation level.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    reason = request.form.get("reason", "").strip()[:1000]
    if len(reason) < 8:
        flash("Add at least 8 characters explaining why the incident needs escalation.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    target = ESCALATION_TARGETS[next_level]
    actor = g.user["full_name"]
    db.execute(
        """INSERT INTO ticket_escalations
        (ticket_id, level, target_role, reason, actor_id, actor_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, next_level, target, reason, g.user["id"], actor, now_iso()),
    )
    db.execute("UPDATE tickets SET escalation_level = ?, updated_at = ? WHERE id = ?",
               (next_level, now_iso(), ticket_id))
    log_activity(db, "Incident escalated", f"#{ticket_id} ? Level {next_level} ? {target}", actor)
    notify_roles(
        db, ("Admin", "IT Agent"), "escalation",
        f"Incident #{ticket_id} escalated",
        f"{actor} escalated ?{ticket['title']}? to {target}. Reason: {reason[:240]}",
        f"/tickets/{ticket_id}", exclude_user_id=g.user["id"],
    )
    notify_ticket_owner(
        db, ticket, "escalation",
        f"Your request #{ticket_id} needs additional support",
        f"IT escalated your request to {target} for further investigation.",
    )
    db.commit()
    flash(f"Incident #{ticket_id} escalated to {target}.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/assets", methods=["GET", "POST"])
@roles_required("Admin", "IT Agent")
def assets_page():
    db = get_db()
    if request.method == "POST":
        tag = request.form.get("asset_tag", "").strip().upper()[:30]
        name = request.form.get("name", "").strip()
        asset_type = request.form.get("asset_type", "Laptop").strip()[:40]
        assigned = request.form.get("assigned_to", "Unassigned").strip()[:100] or "Unassigned"
        department = request.form.get("department", "Operations").strip()[:60] or "Operations"
        status = request.form.get("status", "In stock")
        warranty = request.form.get("warranty_until", "").strip()
        if not tag or not name or status not in ASSET_STATUSES:
            flash("Enter an asset tag and name, and choose a valid status.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO assets (asset_tag, name, asset_type, assigned_to, department, status, warranty_until) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tag, name[:100], asset_type, assigned, department, status, warranty),
                )
                log_activity(db, "Asset registered", f"{tag} ? {name[:80]}", g.user["full_name"])
                db.commit()
                flash(f"Asset {tag} added to the inventory.", "success")
            except sqlite3.IntegrityError:
                flash(f"Asset tag {tag} already exists.", "error")
        return redirect(url_for("assets_page"))
    q = request.args.get("q", "").strip()[:100]
    assets = db.execute(
        "SELECT * FROM assets WHERE asset_tag LIKE ? OR name LIKE ? OR assigned_to LIKE ? ORDER BY id DESC",
        (f"%{q}%", f"%{q}%", f"%{q}%"),
    ).fetchall()
    counts = {s: db.execute("SELECT COUNT(*) FROM assets WHERE status = ?", (s,)).fetchone()[0] for s in ASSET_STATUSES}
    return render_template("assets.html", active_page="assets", assets=assets, counts=counts, q=q)


@app.route("/knowledge")
@login_required
def knowledge_page():
    db = get_db()
    q = request.args.get("q", "").strip()[:100]
    articles = db.execute(
        "SELECT * FROM articles WHERE title LIKE ? OR summary LIKE ? OR category LIKE ? ORDER BY views DESC",
        (f"%{q}%", f"%{q}%", f"%{q}%"),
    ).fetchall()
    return render_template("knowledge.html", active_page="knowledge", articles=articles, q=q)


@app.route("/knowledge/<int:article_id>")
@login_required
def article_detail(article_id: int):
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if article is None:
        abort(404)
    db.execute("UPDATE articles SET views = views + 1 WHERE id = ?", (article_id,))
    db.commit()
    return render_template("article_detail.html", active_page="knowledge", article=article)


@app.route("/notifications")
@login_required
def notifications_page():
    db = get_db()
    notifications = db.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100",
        (g.user["id"],),
    ).fetchall()
    email_rows = []
    if g.user["role"] == "Admin":
        email_rows = db.execute(
            "SELECT * FROM email_outbox ORDER BY id DESC LIMIT 40"
        ).fetchall()
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (g.user["id"],),
    ).fetchone()[0]
    return render_template(
        "notifications.html", active_page="notifications",
        notifications=notifications, email_rows=email_rows, unread=unread,
        smtp_configured=smtp_is_configured(),
    )


@app.post("/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(notification_id: int):
    db = get_db()
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
        (notification_id, g.user["id"]),
    )
    db.commit()
    destination = request.form.get("next", url_for("notifications_page"))
    parsed = urlparse(destination)
    if parsed.netloc or not destination.startswith("/") or destination.startswith("//"):
        destination = url_for("notifications_page")
    return redirect(destination)


@app.post("/notifications/read-all")
@login_required
def mark_all_notifications_read():
    db = get_db()
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (g.user["id"],))
    db.commit()
    flash("All notifications marked as read.", "success")
    return redirect(url_for("notifications_page"))


@app.route("/api/updates")
@roles_required("Admin", "IT Agent")
def live_updates_api():
    db = get_db()
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        after_id = 0
    changes = db.execute(
        "SELECT id, action, detail, actor, created_at FROM activity WHERE id > ? ORDER BY id ASC LIMIT 30",
        (after_id,),
    ).fetchall()
    ticket_updates = db.execute(
        """SELECT id, title, requester, assignee, priority, status, updated_at
        FROM tickets ORDER BY updated_at DESC LIMIT 20"""
    ).fetchall()
    tickets = db.execute("SELECT * FROM tickets").fetchall()
    breached = sum(ticket_sla(ticket) == "breached" for ticket in tickets)
    unread = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (g.user["id"],),
    ).fetchone()[0]
    latest_notifications = db.execute(
        "SELECT id, kind, title, message, href, is_read, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 4",
        (g.user["id"],),
    ).fetchall()
    cursor = db.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    return jsonify({
        "activity": [dict(item) for item in changes],
        "tickets": [dict(item) for item in ticket_updates],
        "notifications": [dict(item) for item in latest_notifications],
        "cursor": cursor,
        "unread": unread,
        "metrics": {
            "total": len(tickets),
            "open": sum(ticket["status"] != "Resolved" for ticket in tickets),
            "breached": breached,
            "resolved": sum(ticket["status"] == "Resolved" for ticket in tickets),
            "sla_health": round(100 * (len(tickets) - breached) / max(1, len(tickets))),
        },
        "server_time": now_iso(),
    })


@app.route("/reports")
@roles_required("Admin", "IT Agent")
def reports_page():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    status_rows = db.execute("SELECT status, COUNT(*) AS total FROM tickets GROUP BY status").fetchall()
    priority_rows = db.execute("SELECT priority, COUNT(*) AS total FROM tickets GROUP BY priority").fetchall()
    category_rows = db.execute("SELECT category, COUNT(*) AS total FROM tickets GROUP BY category ORDER BY total DESC").fetchall()
    recent = db.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 8").fetchall()
    resolved = db.execute("SELECT COUNT(*) FROM tickets WHERE status = 'Resolved'").fetchone()[0]
    breached = sum(ticket_sla(t) == "breached" for t in db.execute("SELECT * FROM tickets").fetchall())
    return render_template("reports.html", active_page="reports", total=total,
                           status_rows=status_rows, priority_rows=priority_rows, category_rows=category_rows,
                           recent=recent, resolved=resolved, breached=breached,
                           resolution_rate=round(resolved * 100 / max(1, total)))


@app.route("/export/tickets.csv")
@roles_required("Admin", "IT Agent")
def export_tickets():
    db = get_db()
    rows = db.execute("SELECT id, title, requester, department, category, priority, status, assignee, created_at, due_at FROM tickets ORDER BY id").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Ticket ID", "Title", "Requester", "Department", "Category", "Priority", "Status", "Assignee", "Created", "SLA due"])
    writer.writerows([tuple(row) for row in rows])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=pulsedesk-tickets.csv"
    return response


@app.route("/api/metrics")
@roles_required("Admin", "IT Agent")
def metrics_api():
    db = get_db()
    tickets = db.execute("SELECT * FROM tickets").fetchall()
    return jsonify({
        "total": len(tickets),
        "open": sum(t["status"] != "Resolved" for t in tickets),
        "breached": sum(ticket_sla(t) == "breached" for t in tickets),
        "sla_health": round(100 * (len(tickets) - sum(ticket_sla(t) == "breached" for t in tickets)) / max(1, len(tickets))),
        "by_status": {status: sum(t["status"] == status for t in tickets) for status in STATUSES},
        "by_priority": {priority: sum(t["priority"] == priority for t in tickets) for priority in PRIORITIES},
    })


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", active_page=""), 404

@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", active_page=""), 403


@app.errorhandler(413)
def too_large(_error):
    flash("That submission is too large. Please keep attachments under 2 MB.", "error")
    return redirect(request.referrer or url_for("dashboard"))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
