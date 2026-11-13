from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
from email.message import EmailMessage
from functools import wraps
from html import escape
from urllib.parse import urlparse
from datetime import datetime, timedelta, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        json_payload = request.get_json(silent=True) or {}
        submitted = (
            request.form.get("csrf_token", "")
            or request.headers.get("X-CSRF-Token", "")
            or (json_payload.get("csrf_token", "") if isinstance(json_payload, dict) else "")
        )
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


_ASSISTANT_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "when", "where",
    "have", "has", "had", "are", "was", "were", "our", "their", "they",
    "them", "into", "after", "before", "please", "help", "need", "unable",
    "not", "but", "you", "your", "can", "cannot", "could", "would", "should",
    "issue", "problem", "user", "users", "team", "work", "working",
}


def _assistant_terms(value: str) -> set[str]:
    return {
        term for term in re.findall(r"[a-z0-9]{3,}", (value or "").lower())
        if term not in _ASSISTANT_STOP_WORDS
    }


def match_knowledge_articles(
    db: sqlite3.Connection, query: str, category: str = ""
) -> list[dict]:
    """Rank existing runbooks by transparent keyword overlap and category match."""
    terms = _assistant_terms(query)
    articles = db.execute("SELECT id, title, category, summary, content FROM articles").fetchall()
    ranked = []
    for article in articles:
        title_terms = _assistant_terms(article["title"])
        summary_terms = _assistant_terms(article["summary"])
        content_terms = _assistant_terms(article["content"])
        title_hits = terms & title_terms
        summary_hits = terms & summary_terms
        content_hits = terms & content_terms
        category_match = bool(category and article["category"].lower() == category.lower())
        score = len(title_hits) * 4 + len(summary_hits) * 2 + len(content_hits)
        if category_match:
            score += 2
        if score < 2:
            continue
        matched_terms = sorted(title_hits | summary_hits | content_hits)
        ranked.append({
            "id": article["id"],
            "title": article["title"],
            "category": article["category"],
            "summary": article["summary"],
            "score": score,
            "matched_terms": matched_terms[:6],
            "category_match": category_match,
        })
    ranked.sort(key=lambda item: (-item["score"], -len(item["matched_terms"]), item["title"].lower()))
    return ranked[:4]


def analyse_incident(
    db: sqlite3.Connection, title: str, description: str, category: str
) -> dict:
    """Create a human-reviewable triage brief; this is rule-based, not an AI model."""
    title = (title or "").strip()[:140]
    description = (description or "").strip()[:2000]
    category = category if category in CATEGORIES else "Other"
    text = f"{title} {description}".lower()
    suggested_priority, base_reason = recommend_priority(title, description, category)

    impact_rules = [
        ("Service outage", ("outage", "server down", "service unavailable", "production down"), "A service outage phrase was found."),
        ("Broad impact", ("all users", "whole team", "multiple departments", "company-wide", "everyone affected"), "The description suggests multiple people or teams may be affected."),
        ("Security signal", ("ransomware", "data breach", "data loss", "suspicious sign-in", "phishing", "malware", "unauthorised access", "unauthorized access"), "A security-related phrase needs prompt human review."),
        ("Work blocked", ("cannot work", "can't work", "production blocked", "release blocked", "locked out", "business blocked"), "A user or workflow may be blocked."),
    ]
    signals = []
    for label, phrases, explanation in impact_rules:
        if any(phrase in text for phrase in phrases):
            signals.append({"label": label, "explanation": explanation})

    if any(signal["label"] in ("Service outage", "Broad impact") for signal in signals):
        suggested_priority = "Critical"
        reason = "Possible service-wide impact detected. Confirm scope and business impact immediately."
    elif any(signal["label"] == "Security signal" for signal in signals):
        suggested_priority = "High" if suggested_priority != "Critical" else suggested_priority
        reason = "A possible security indicator was detected. Follow the organisation's security response process."
    else:
        reason = base_reason

    if suggested_priority == "Critical":
        response_target = "Immediately"
    elif suggested_priority == "High":
        response_target = "Within 1 hour"
    elif suggested_priority == "Medium":
        response_target = "Within 4 business hours"
    else:
        response_target = "Within 1 business day"

    if category == "Network":
        checks = [
            "Confirm whether the issue affects one device, one location or multiple teams.",
            "Capture the affected network/VPN, exact time, error message and connection type.",
            "Check approved service-health and network-monitoring dashboards before changing configuration.",
        ]
    elif category == "Access":
        checks = [
            "Verify the user's identity through the approved company process.",
            "Check account status, assigned role and recent access changes.",
            "Never ask for passwords, MFA codes or recovery codes; use the approved account-recovery workflow.",
        ]
    elif category == "Email":
        checks = [
            "Establish whether sending, receiving or both are affected and identify the affected mailboxes.",
            "Capture one example timestamp and message ID, where available.",
            "Check approved mail-service health and message-trace tools.",
        ]
    elif category == "Hardware":
        checks = [
            "Record the device asset tag, model and assigned user.",
            "Check power, cabling, peripherals and any visible error indicators.",
            "Confirm whether a safe restart or known-good peripheral test is appropriate.",
        ]
    elif category == "Software":
        checks = [
            "Record the application version, device and exact error or failing workflow.",
            "Check recent releases, maintenance notices and known issues.",
            "Compare whether the problem affects one user or multiple users before changing settings.",
        ]
    else:
        checks = [
            "Confirm the affected users, device/service and business workflow.",
            "Record when the problem started and capture the exact error message.",
            "Check recent changes and known service issues before attempting a fix.",
        ]

    if any(signal["label"] == "Security signal" for signal in signals):
        checks = [
            "Do not approve unexpected MFA prompts or open suspicious links or attachments.",
            "Preserve the alert details and notify the authorised security contact immediately.",
            "Follow the organisation's incident-response procedure; do not attempt ad-hoc evidence cleanup.",
        ] + checks[:1]

    article_matches = match_knowledge_articles(db, f"{title} {description}", category)
    return {
        "title": title,
        "category": category,
        "suggested_priority": suggested_priority,
        "reason": reason,
        "response_target": response_target,
        "signals": signals,
        "checks": checks,
        "articles": article_matches,
        "review_note": "Decision support only. A human IT responder must confirm impact, urgency and the appropriate response with the requester and your IT/security process.",
        "method": "Transparent keyword and category rules",
    }


RECURRING_THEMES = (
    ("VPN & connectivity", ("vpn", "wifi", "wi-fi", "network", "connection", "disconnect", "dns", "internet", "latency")),
    ("Access & identity", ("access", "login", "locked", "mfa", "authenticator", "password", "permission", "sign-in", "signin")),
    ("Email & messaging", ("email", "mail", "outlook", "delivery", "mailbox", "calendar")),
    ("Devices & hardware", ("laptop", "printer", "keyboard", "monitor", "device", "display", "hardware", "screen")),
    ("Business applications", ("application", "software", "crm", "portal", "licence", "license", "install", "crash")),
    ("Security review", ("suspicious", "phishing", "ransomware", "breach", "malware", "security", "unauthorised", "unauthorized")),
)


def detect_recurring_incidents(
    tickets: list[sqlite3.Row] | list[dict], days: int = 90
) -> list[dict]:
    """Group incidents by explicit keyword themes; results are signals, not duplicate verdicts."""
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    groups: dict[str, list[dict]] = {}
    for ticket in tickets:
        try:
            created = datetime.fromisoformat(ticket["created_at"])
        except (TypeError, ValueError):
            continue
        if created < cutoff or created > now:
            continue
        text = f"{ticket['title']} {ticket['description']}".lower()
        matches = []
        for label, keywords in RECURRING_THEMES:
            hits = [word for word in keywords if word in text]
            if hits:
                matches.append((len(hits), label, hits))
        if not matches:
            category = ticket["category"]
            if category in ("Network", "Access", "Email", "Hardware", "Software"):
                matches = [(1, f"{category} support", [category.lower()])]
        if not matches:
            continue
        _score, label, hits = sorted(matches, key=lambda item: (-item[0], item[1]))[0]
        groups.setdefault(label, []).append({
            "id": ticket["id"],
            "title": ticket["title"],
            "status": ticket["status"],
            "priority": ticket["priority"],
            "assignee": ticket["assignee"],
            "matched_terms": hits,
        })

    patterns = []
    for label, items in groups.items():
        if len(items) < 2:
            continue
        patterns.append({
            "theme": label,
            "count": len(items),
            "open_count": sum(item["status"] != "Resolved" for item in items),
            "critical_count": sum(item["priority"] == "Critical" and item["status"] != "Resolved" for item in items),
            "ticket_ids": [item["id"] for item in items],
            "samples": items[:3],
            "matched_terms": sorted({term for item in items for term in item["matched_terms"]})[:8],
        })
    patterns.sort(key=lambda item: (-item["open_count"], -item["count"], item["theme"]))
    return patterns[:6]


def build_daily_operations_summary(db: sqlite3.Connection) -> dict:
    """Summarise today's actual ticket and activity records using local server time."""
    today = datetime.now().date()
    tickets = db.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()

    def day_of(value: str | None):
        try:
            return datetime.fromisoformat(value).date() if value else None
        except (TypeError, ValueError):
            return None

    created_today = [ticket for ticket in tickets if day_of(ticket["created_at"]) == today]
    resolved_today = [
        ticket for ticket in tickets
        if ticket["status"] == "Resolved"
        and day_of(ticket["resolved_at"] or ticket["updated_at"]) == today
    ]
    active = [ticket for ticket in tickets if ticket["status"] != "Resolved"]
    overdue = [ticket for ticket in active if ticket_sla(ticket) == "breached"]
    critical = [ticket for ticket in active if ticket["priority"] == "Critical"]
    unassigned = [ticket for ticket in active if (ticket["assignee"] or "").strip().lower() == "unassigned"]
    patterns = detect_recurring_incidents(tickets, 90)
    activity = db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 6").fetchall()

    category_counts: dict[str, int] = {}
    for ticket in active:
        category_counts[ticket["category"]] = category_counts.get(ticket["category"], 0) + 1
    top_category = max(category_counts, key=category_counts.get) if category_counts else "None"

    highlights = []
    if critical:
        highlights.append(f"{len(critical)} active critical incident(s) need immediate human review.")
    if overdue:
        highlights.append(f"{len(overdue)} active incident(s) are past their SLA due time.")
    if unassigned:
        highlights.append(f"{len(unassigned)} active incident(s) do not have an assignee.")
    if patterns:
        highlights.append(f"{len(patterns)} recurring theme(s) appear in the 90-day incident sample.")
    if not highlights:
        highlights.append("No critical, overdue or unassigned incidents were found in the current register.")

    return {
        "date_label": today.strftime("%A, %d %B %Y"),
        "total": len(tickets),
        "created_today": len(created_today),
        "resolved_today": len(resolved_today),
        "active": len(active),
        "critical": len(critical),
        "overdue": len(overdue),
        "unassigned": len(unassigned),
        "top_category": top_category,
        "patterns": patterns,
        "highlights": highlights,
        "activity": [dict(item) for item in activity],
    }


def write_audit_event(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: int | str,
    action: str,
    summary: str,
    actor_id: int | None,
    actor_name: str,
    metadata: dict | None = None,
) -> None:
    """Append a structured event; database triggers prevent update/delete."""
    db.execute(
        """INSERT INTO audit_events
        (entity_type, entity_id, action, summary, actor_id, actor_name, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_type[:40], str(entity_id)[:80], action[:60], summary[:1000],
            actor_id, actor_name[:100], json.dumps(metadata or {}, ensure_ascii=False)[:4000], now_iso(),
        ),
    )


def write_asset_event(
    db: sqlite3.Connection,
    asset_id: int,
    action: str,
    summary: str,
    actor_id: int | None,
    actor_name: str,
    metadata: dict | None = None,
) -> None:
    """Append an immutable lifecycle event and mirror it to the global audit register."""
    payload = metadata or {}
    db.execute(
        """INSERT INTO asset_events
        (asset_id, action, summary, actor_id, actor_name, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (asset_id, action[:60], summary[:1000], actor_id, actor_name[:100],
         json.dumps(payload, ensure_ascii=False)[:4000], now_iso()),
    )
    write_audit_event(
        db, "asset", asset_id, action, summary, actor_id, actor_name, payload
    )


def asset_lifecycle_metrics(db: sqlite3.Connection) -> dict:
    today = datetime.now().date()
    horizon = (today + timedelta(days=60)).isoformat()
    current = today.isoformat()
    expiring = db.execute(
        """SELECT COUNT(*) FROM assets
        WHERE warranty_until != '' AND warranty_until >= ? AND warranty_until <= ? AND status != 'Retired'""",
        (current, horizon),
    ).fetchone()[0]
    expired = db.execute(
        """SELECT COUNT(*) FROM assets
        WHERE warranty_until != '' AND warranty_until < ? AND status != 'Retired'""",
        (current,),
    ).fetchone()[0]
    maintenance_due = db.execute(
        """SELECT COUNT(*) FROM assets
        WHERE status != 'Retired' AND next_maintenance_at != '' AND next_maintenance_at <= ?""",
        (current,),
    ).fetchone()[0]
    maintenance_upcoming = db.execute(
        """SELECT COUNT(*) FROM assets
        WHERE status != 'Retired' AND next_maintenance_at > ? AND next_maintenance_at <= ?""",
        (current, horizon),
    ).fetchone()[0]
    unassigned = db.execute(
        "SELECT COUNT(*) FROM assets WHERE assigned_to='Unassigned' AND status != 'Retired'"
    ).fetchone()[0]
    return {
        "total": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        "in_use": db.execute("SELECT COUNT(*) FROM assets WHERE status='In use'").fetchone()[0],
        "in_stock": db.execute("SELECT COUNT(*) FROM assets WHERE status='In stock'").fetchone()[0],
        "in_repair": db.execute("SELECT COUNT(*) FROM assets WHERE status='In repair'").fetchone()[0],
        "retired": db.execute("SELECT COUNT(*) FROM assets WHERE status='Retired'").fetchone()[0],
        "warranty_expiring": expiring,
        "warranty_expired": expired,
        "maintenance_due": maintenance_due,
        "maintenance_upcoming": maintenance_upcoming,
        "unassigned": unassigned,
    }


def decorate_asset(asset: sqlite3.Row | dict) -> dict:
    item = dict(asset)
    today = datetime.now().date()
    warranty_state = "unknown"
    warranty_days = None
    if item.get("warranty_until"):
        try:
            warranty_date = datetime.fromisoformat(item["warranty_until"]).date()
            warranty_days = (warranty_date - today).days
            warranty_state = "expired" if warranty_days < 0 else ("expiring" if warranty_days <= 60 else "active")
        except (TypeError, ValueError):
            warranty_state = "invalid"
    maintenance_state = "not-scheduled"
    maintenance_days = None
    if item.get("next_maintenance_at"):
        try:
            maintenance_date = datetime.fromisoformat(item["next_maintenance_at"]).date()
            maintenance_days = (maintenance_date - today).days
            maintenance_state = "due" if maintenance_days <= 0 else ("upcoming" if maintenance_days <= 30 else "scheduled")
        except (TypeError, ValueError):
            maintenance_state = "invalid"
    item["warranty_state"] = warranty_state
    item["warranty_days"] = warranty_days
    item["maintenance_state"] = maintenance_state
    item["maintenance_days"] = maintenance_days
    return item


def default_workflow_rules() -> list[tuple[str, int, int, str]]:
    return [
        ("Critical", 1, 0, ESCALATION_TARGETS[1]),
        ("Critical", 2, 30, ESCALATION_TARGETS[2]),
        ("Critical", 3, 90, ESCALATION_TARGETS[3]),
        ("High", 1, 30, ESCALATION_TARGETS[1]),
        ("High", 2, 120, ESCALATION_TARGETS[2]),
        ("High", 3, 360, ESCALATION_TARGETS[3]),
        ("Medium", 1, 240, ESCALATION_TARGETS[1]),
        ("Medium", 2, 480, ESCALATION_TARGETS[2]),
        ("Medium", 3, 1440, ESCALATION_TARGETS[3]),
        ("Low", 1, 1440, ESCALATION_TARGETS[1]),
        ("Low", 2, 2880, ESCALATION_TARGETS[2]),
        ("Low", 3, 4320, ESCALATION_TARGETS[3]),
    ]


def suggest_ticket_assignees(
    db: sqlite3.Connection, ticket: sqlite3.Row
) -> list[dict]:
    """Rank active Admin/IT Agent accounts by weighted live workload."""
    candidates = db.execute(
        "SELECT id, full_name, role FROM users WHERE active = 1 AND role IN ('Admin', 'IT Agent') ORDER BY full_name"
    ).fetchall()
    suggestions = []
    for person in candidates:
        workload = db.execute(
            """SELECT
                COUNT(*) AS active,
                SUM(CASE WHEN priority = 'Critical' THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN due_at < ? THEN 1 ELSE 0 END) AS overdue,
                SUM(CASE WHEN category = ? THEN 1 ELSE 0 END) AS same_category
            FROM tickets
            WHERE status != 'Resolved' AND assignee = ? AND id != ?""",
            (now_iso(), ticket["category"], person["full_name"], ticket["id"]),
        ).fetchone()
        active = int(workload["active"] or 0)
        critical = int(workload["critical"] or 0)
        overdue = int(workload["overdue"] or 0)
        same_category = int(workload["same_category"] or 0)
        score = active * 3 + critical * 5 + overdue * 3
        suggestions.append({
            "id": person["id"],
            "name": person["full_name"],
            "role": person["role"],
            "active": active,
            "critical": critical,
            "overdue": overdue,
            "same_category": same_category,
            "score": score,
        })
    suggestions.sort(key=lambda item: (item["score"], item["active"], item["name"].lower()))
    if suggestions:
        best = suggestions[0]
        best["reason"] = (
            f"Lowest weighted workload: {best['active']} active, "
            f"{best['critical']} critical and {best['overdue']} overdue."
        )
        for item in suggestions[1:]:
            item["reason"] = (
                f"{item['active']} active, {item['critical']} critical and "
                f"{item['overdue']} overdue."
            )
    return suggestions


def get_sla_calendar(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT * FROM sla_calendar WHERE id = 1").fetchone()
    if row is None:
        return {
            "timezone_name": "Europe/Riga", "business_start": "09:00",
            "business_end": "17:00", "weekdays": "0,1,2,3,4",
            "scheduler_enabled": 0, "interval_seconds": 60,
        }
    result = dict(row)
    result["weekday_numbers"] = {
        int(value) for value in result["weekdays"].split(",") if value.strip().isdigit()
    }
    return result


def calendar_now(calendar: dict) -> datetime:
    return datetime.now(ZoneInfo(calendar["timezone_name"])).replace(tzinfo=None, microsecond=0)


def business_minutes_between(
    start: datetime, end: datetime, calendar: dict
) -> int:
    """Count working minutes in the configured weekly calendar, excluding weekends/off-hours."""
    if end <= start:
        return 0
    weekdays = calendar.get("weekday_numbers")
    if weekdays is None:
        weekdays = {
            int(value) for value in calendar.get("weekdays", "0,1,2,3,4").split(",")
            if value.strip().isdigit()
        }
    day_start = time.fromisoformat(calendar.get("business_start", "09:00"))
    day_end = time.fromisoformat(calendar.get("business_end", "17:00"))
    if day_start >= day_end:
        return 0
    total_seconds = 0
    days = (end.date() - start.date()).days
    for offset in range(days + 1):
        current_date = start.date() + timedelta(days=offset)
        if current_date.weekday() not in weekdays:
            continue
        window_start = datetime.combine(current_date, day_start)
        window_end = datetime.combine(current_date, day_end)
        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)
        if overlap_end > overlap_start:
            total_seconds += (overlap_end - overlap_start).total_seconds()
    return int(total_seconds // 60)


def worker_status_snapshot(db: sqlite3.Connection) -> dict:
    state = db.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    calendar = get_sla_calendar(db)
    if state is None:
        return {"online": False, "status": "offline", "heartbeat_age_seconds": None}
    heartbeat_age = None
    if state["last_heartbeat"]:
        try:
            heartbeat_age = max(
                0, int((datetime.now() - datetime.fromisoformat(state["last_heartbeat"])).total_seconds())
            )
        except ValueError:
            heartbeat_age = None
    online = (
        heartbeat_age is not None and heartbeat_age <= 35
        and state["status"] in ("starting", "running", "paused", "error")
    )
    result = dict(state)
    result["online"] = online
    result["status"] = state["status"] if online else "offline"
    result["heartbeat_age_seconds"] = heartbeat_age
    result["scheduler_enabled"] = bool(calendar["scheduler_enabled"])
    return result


def run_sla_escalation_check(
    db: sqlite3.Connection, actor_id: int | None, actor_name: str
) -> dict:
    """Process only the next configured escalation stage for each eligible ticket.

    Acquire a write reservation before reading the queue so manual and scheduled checks
    cannot both apply the same escalation stage at the same time.
    """
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    rules = db.execute(
        "SELECT * FROM workflow_rules WHERE is_active = 1 ORDER BY overdue_minutes, escalation_level"
    ).fetchall()
    tickets = db.execute(
        "SELECT * FROM tickets WHERE status != 'Resolved' ORDER BY due_at ASC"
    ).fetchall()
    calendar = get_sla_calendar(db)
    current = calendar_now(calendar)
    escalated = []
    skipped = 0
    for ticket in tickets:
        try:
            due_at = datetime.fromisoformat(ticket["due_at"])
        except (TypeError, ValueError):
            skipped += 1
            continue
        if due_at >= current:
            continue
        overdue_minutes = business_minutes_between(due_at, current, calendar)
        if overdue_minutes == 0:
            continue
        next_level = int(ticket["escalation_level"] or 0) + 1
        rule = next((
            item for item in rules
            if item["priority"] == ticket["priority"]
            and item["escalation_level"] == next_level
            and overdue_minutes >= item["overdue_minutes"]
        ), None)
        if not rule:
            continue

        target = rule["target_role"]
        reason = (
            f"Automated SLA workflow rule #{rule['id']}: "
            f"{overdue_minutes} minute(s) past the due time "
            f"(threshold {rule['overdue_minutes']} minutes)."
        )
        db.execute(
            """INSERT INTO ticket_escalations
            (ticket_id, level, target_role, reason, actor_id, actor_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ticket["id"], next_level, target, reason, actor_id, actor_name, now_iso()),
        )
        db.execute(
            "UPDATE tickets SET escalation_level = ?, updated_at = ? WHERE id = ?",
            (next_level, now_iso(), ticket["id"]),
        )
        log_activity(
            db, "SLA workflow escalated",
            f"#{ticket['id']} → Level {next_level} → {target}", actor_name,
        )
        write_audit_event(
            db, "ticket", ticket["id"], "SLA escalation",
            f"Incident #{ticket['id']} escalated to {target} by workflow rule #{rule['id']}.",
            actor_id, actor_name,
            {"rule_id": rule["id"], "priority": ticket["priority"],
             "overdue_minutes": overdue_minutes, "level": next_level},
        )
        notify_roles(
            db, ("Admin", "IT Agent"), "escalation",
            f"Incident #{ticket['id']} escalated by SLA workflow",
            f"PulseDesk advanced {ticket['title']} to {target}. {reason}",
            f"/tickets/{ticket['id']}", exclude_user_id=actor_id,
        )
        notify_ticket_owner(
            db, ticket, "escalation",
            f"Your request #{ticket['id']} needs additional support",
            f"IT advanced your request to {target} under the configured SLA workflow.",
        )
        escalated.append({
            "ticket_id": ticket["id"], "title": ticket["title"],
            "priority": ticket["priority"], "level": next_level,
            "target": target, "overdue_minutes": overdue_minutes,
        })

    return {
        "checked": len(tickets),
        "escalated": escalated,
        "escalated_count": len(escalated),
        "skipped_invalid_due_dates": skipped,
        "checked_at": now_iso(),
    }


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
                escalation_level INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT
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
                warranty_until TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                vendor TEXT NOT NULL DEFAULT '',
                purchase_date TEXT NOT NULL DEFAULT '',
                purchase_cost REAL,
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                next_maintenance_at TEXT NOT NULL DEFAULT '',
                maintenance_interval_days INTEGER NOT NULL DEFAULT 180,
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS asset_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                summary TEXT NOT NULL,
                actor_id INTEGER REFERENCES users(id),
                actor_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS asset_maintenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                task TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Scheduled' CHECK(status IN ('Scheduled','Completed','Cancelled')),
                outcome TEXT NOT NULL DEFAULT '',
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                completed_by INTEGER REFERENCES users(id),
                completed_at TEXT
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

            CREATE TABLE IF NOT EXISTS workflow_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                priority TEXT NOT NULL CHECK(priority IN ('Low','Medium','High','Critical')),
                escalation_level INTEGER NOT NULL CHECK(escalation_level BETWEEN 1 AND 3),
                overdue_minutes INTEGER NOT NULL DEFAULT 0 CHECK(overdue_minutes BETWEEN 0 AND 43200),
                target_role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER REFERENCES users(id),
                updated_at TEXT NOT NULL,
                UNIQUE(priority, escalation_level)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT NOT NULL,
                actor_id INTEGER REFERENCES users(id),
                actor_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sla_calendar (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                timezone_name TEXT NOT NULL DEFAULT 'Europe/Riga',
                business_start TEXT NOT NULL DEFAULT '09:00',
                business_end TEXT NOT NULL DEFAULT '17:00',
                weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4',
                scheduler_enabled INTEGER NOT NULL DEFAULT 0,
                interval_seconds INTEGER NOT NULL DEFAULT 60,
                updated_by INTEGER REFERENCES users(id),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                owner_token TEXT,
                worker_name TEXT NOT NULL DEFAULT 'PulseDesk SLA Worker',
                status TEXT NOT NULL DEFAULT 'offline',
                started_at TEXT,
                last_heartbeat TEXT,
                last_run TEXT,
                last_result TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                lease_until TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
            CREATE INDEX IF NOT EXISTS idx_tickets_due ON tickets(due_at);
            CREATE INDEX IF NOT EXISTS idx_escalations_ticket ON ticket_escalations(ticket_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_escalations_ticket_level_unique
            ON ticket_escalations(ticket_id, level);
            CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, id);
            CREATE INDEX IF NOT EXISTS idx_activity_id ON activity(id);
            CREATE INDEX IF NOT EXISTS idx_email_outbox_created ON email_outbox(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_asset_events_asset ON asset_events(asset_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_asset_maintenance_due ON asset_maintenance(status, due_at);
            CREATE TRIGGER IF NOT EXISTS asset_events_no_update
            BEFORE UPDATE ON asset_events
            BEGIN SELECT RAISE(ABORT, 'Asset events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS asset_events_no_delete
            BEFORE DELETE ON asset_events
            BEGIN SELECT RAISE(ABORT, 'Asset events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'Audit events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'Audit events are immutable'); END;
            """
        )
        asset_columns = {row["name"] for row in db.execute("PRAGMA table_info(assets)")}
        asset_migrations = (
            ("serial_number", "TEXT NOT NULL DEFAULT ''"),
            ("vendor", "TEXT NOT NULL DEFAULT ''"),
            ("purchase_date", "TEXT NOT NULL DEFAULT ''"),
            ("purchase_cost", "REAL"),
            ("location", "TEXT NOT NULL DEFAULT ''"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            ("next_maintenance_at", "TEXT NOT NULL DEFAULT ''"),
            ("maintenance_interval_days", "INTEGER NOT NULL DEFAULT 180"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        )
        for column_name, column_type in asset_migrations:
            if column_name not in asset_columns:
                db.execute(f"ALTER TABLE assets ADD COLUMN {column_name} {column_type}")
        db.execute(
            "INSERT INTO asset_events (asset_id, action, summary, actor_id, actor_name, metadata_json, created_at) "
            "SELECT a.id, 'Inventory baseline', 'Existing inventory record imported into lifecycle tracking.', "
            "NULL, 'PulseDesk Migration', '{}', ? FROM assets a "
            "WHERE NOT EXISTS (SELECT 1 FROM asset_events e WHERE e.asset_id=a.id)",
            (now_iso(),),
        )

        ticket_columns = {row["name"] for row in db.execute("PRAGMA table_info(tickets)")}
        if "created_by" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN created_by INTEGER REFERENCES users(id)")
        if "escalation_level" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN escalation_level INTEGER NOT NULL DEFAULT 0")
        if "resolved_at" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN resolved_at TEXT")

        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for email, full_name, role, password in DEMO_ACCOUNTS:
                db.execute(
                    "INSERT INTO users (email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, full_name, generate_password_hash(password), role, now_iso()),
                )
        db.execute(
            """INSERT OR IGNORE INTO sla_calendar
            (id, timezone_name, business_start, business_end, weekdays, scheduler_enabled, interval_seconds, updated_at)
            VALUES (1, 'Europe/Riga', '09:00', '17:00', '0,1,2,3,4', 0, 60, ?)""",
            (now_iso(),),
        )
        db.execute(
            """INSERT OR IGNORE INTO worker_state
            (id, worker_name, status, last_result, last_error)
            VALUES (1, 'PulseDesk SLA Worker', 'offline', '', '')"""
        )
        if db.execute("SELECT COUNT(*) FROM workflow_rules").fetchone()[0] == 0:
            db.executemany(
                """INSERT INTO workflow_rules
                (priority, escalation_level, overdue_minutes, target_role, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                [(priority, level, minutes, target, now_iso())
                 for priority, level, minutes, target in default_workflow_rules()],
            )
        if db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0:
            seed_demo_data(db)
        db.execute(
            "INSERT INTO asset_events (asset_id, action, summary, actor_id, actor_name, metadata_json, created_at) "
            "SELECT a.id, 'Inventory baseline', 'Existing inventory record imported into lifecycle tracking.', "
            "NULL, 'PulseDesk Migration', '{}', ? FROM assets a "
            "WHERE NOT EXISTS (SELECT 1 FROM asset_events e WHERE e.asset_id=a.id)",
            (now_iso(),),
        )
        if db.execute("SELECT COUNT(*) FROM asset_maintenance").fetchone()[0] == 0:
            demo_maintenance = (
                ("AST-001", "Endpoint health and security check", 14),
                ("AST-004", "Battery health and device diagnostics", 30),
                ("AST-006", "Printer roller and paper-feed inspection", 7),
            )
            for tag, task, days_ahead in demo_maintenance:
                asset = db.execute("SELECT id FROM assets WHERE asset_tag=?", (tag,)).fetchone()
                if asset:
                    due = (datetime.now().date() + timedelta(days=days_ahead)).isoformat()
                    db.execute(
                        """INSERT INTO asset_maintenance
                        (asset_id, task, due_at, status, created_at)
                        VALUES (?, ?, ?, 'Scheduled', ?)""",
                        (asset["id"], task, due, now_iso()),
                    )
                    db.execute(
                        "UPDATE assets SET next_maintenance_at=?, updated_at=? WHERE id=?",
                        (due, now_iso(), asset["id"]),
                    )
        db.execute(
            """INSERT INTO asset_events
            (asset_id, action, summary, actor_id, actor_name, metadata_json, created_at)
            SELECT m.asset_id, 'Maintenance scheduled',
                m.task || ' · due ' || m.due_at, m.created_by,
                'PulseDesk Migration', '{}', m.created_at
            FROM asset_maintenance m
            WHERE NOT EXISTS (
                SELECT 1 FROM asset_events e
                WHERE e.asset_id=m.asset_id AND e.action='Maintenance scheduled'
                AND e.summary=m.task || ' · due ' || m.due_at
            )"""
        )
        # Backfill historical demo/legacy resolved rows without replacing any ticket data.
        db.execute(
            "UPDATE tickets SET resolved_at = updated_at WHERE status = 'Resolved' AND resolved_at IS NULL"
        )
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
    asset_metrics = asset_lifecycle_metrics(db)
    asset_attention = (
        asset_metrics["warranty_expiring"] + asset_metrics["warranty_expired"]
        + asset_metrics["maintenance_due"]
    )
    return render_template(
        "dashboard.html", active_page="overview", total=len(tickets), open_count=open_count,
        breached=len(breached), critical=critical, resolved=resolved, active=active,
        priorities=priorities, recent=recent, activity=activity, asset_count=assets,
        asset_metrics=asset_metrics, asset_attention=asset_attention,
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
        write_audit_event(
            db, "ticket", cursor.lastrowid, "Ticket created",
            f"Incident #{cursor.lastrowid} created with {priority} priority and {category} category.",
            g.user["id"], g.user["full_name"],
            {"priority": priority, "category": category, "assignee": assignee},
        )
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
        updated_at = now_iso()
        if status == "Resolved":
            resolved_at = ticket["resolved_at"] or updated_at
        else:
            resolved_at = None
        db.execute(
            "UPDATE tickets SET status = ?, priority = ?, assignee = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
            (status, priority, assignee or "Unassigned", updated_at, resolved_at, ticket_id),
        )
        log_activity(db, "Ticket updated", f"#{ticket_id} ? status: {status} ? priority: {priority}", g.user["full_name"])
        changes = {}
        for field, new_value in (("status", status), ("priority", priority), ("assignee", assignee or "Unassigned")):
            if ticket[field] != new_value:
                changes[field] = {"from": ticket[field], "to": new_value}
        if changes:
            write_audit_event(
                db, "ticket", ticket_id, "Ticket updated",
                f"Incident #{ticket_id} changed: " + ", ".join(
                    f"{field} {values['from']} → {values['to']}" for field, values in changes.items()
                ),
                g.user["id"], g.user["full_name"], changes,
            )
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
    assignment_suggestions = (
        suggest_ticket_assignees(db, ticket) if g.user["role"] in ("Admin", "IT Agent") else []
    )
    audit_history = db.execute(
        """SELECT * FROM audit_events WHERE entity_type='ticket' AND entity_id=?
        ORDER BY id DESC LIMIT 8""", (str(ticket_id),)
    ).fetchall()
    return render_template("ticket_detail.html", active_page="tickets", ticket=ticket,
                           sla=ticket_sla(ticket), suggested_priority=suggested_priority, triage_reason=triage_reason,
                           escalation_history=escalation_history, escalation_targets=ESCALATION_TARGETS,
                           assignment_suggestions=assignment_suggestions, audit_history=audit_history)


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
    write_audit_event(
        db, "ticket", ticket_id, "Manual escalation",
        f"Incident #{ticket_id} manually escalated to {target}.",
        g.user["id"], actor, {"level": next_level, "target_role": target, "reason": reason},
    )
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
        name = request.form.get("name", "").strip()[:100]
        asset_type = request.form.get("asset_type", "Laptop").strip()[:40]
        assigned = request.form.get("assigned_to", "Unassigned").strip()[:100] or "Unassigned"
        department = request.form.get("department", "Operations").strip()[:60] or "Operations"
        status = request.form.get("status", "In stock")
        warranty = request.form.get("warranty_until", "").strip()
        serial = request.form.get("serial_number", "").strip()[:100]
        vendor = request.form.get("vendor", "").strip()[:100]
        purchase_date = request.form.get("purchase_date", "").strip()
        location = request.form.get("location", "").strip()[:120]
        next_maintenance = request.form.get("next_maintenance_at", "").strip()
        notes = request.form.get("notes", "").strip()[:2000]
        try:
            purchase_cost = float(request.form.get("purchase_cost", "").strip()) if request.form.get("purchase_cost", "").strip() else None
            interval = int(request.form.get("maintenance_interval_days", "180"))
            for label, value in (("Warranty end", warranty), ("Purchase date", purchase_date), ("Next maintenance", next_maintenance)):
                if value:
                    datetime.fromisoformat(value)
        except (TypeError, ValueError):
            flash("Enter valid dates, a numeric purchase cost and a whole-number maintenance interval.", "error")
            return redirect(url_for("assets_page"))
        if not tag or not name or status not in ASSET_STATUSES or purchase_cost is not None and purchase_cost < 0 or not 1 <= interval <= 3650:
            flash("Check the required asset details, purchase cost and maintenance interval (1–3,650 days).", "error")
            return redirect(url_for("assets_page"))
        try:
            cursor = db.execute(
                """INSERT INTO assets
                (asset_tag, name, asset_type, assigned_to, department, status, warranty_until,
                 serial_number, vendor, purchase_date, purchase_cost, location, notes,
                 next_maintenance_at, maintenance_interval_days, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tag, name, asset_type, assigned, department, status, warranty, serial, vendor,
                 purchase_date, purchase_cost, location, notes, next_maintenance, interval, now_iso()),
            )
            asset_id = cursor.lastrowid
            write_asset_event(
                db, asset_id, "Asset registered", f"{tag} · {name} added to the inventory.",
                g.user["id"], g.user["full_name"],
                {"asset_tag": tag, "type": asset_type, "assigned_to": assigned, "status": status},
            )
            log_activity(db, "Asset registered", f"{tag} · {name[:80]}", g.user["full_name"])
            db.commit()
            flash(f"Asset {tag} added to the inventory.", "success")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        except sqlite3.IntegrityError:
            db.rollback()
            flash(f"Asset tag {tag} already exists.", "error")
            return redirect(url_for("assets_page"))

    q = request.args.get("q", "").strip()[:100]
    watch = request.args.get("watch", "")
    clauses = []
    params = []
    if q:
        clauses.append("(a.asset_tag LIKE ? OR a.name LIKE ? OR a.assigned_to LIKE ? OR a.serial_number LIKE ? OR a.vendor LIKE ?)")
        params.extend([f"%{q}%"] * 5)
    today = datetime.now().date().isoformat()
    horizon = (datetime.now().date() + timedelta(days=60)).isoformat()
    if watch == "warranty":
        clauses.append("a.status != 'Retired' AND a.warranty_until != '' AND a.warranty_until <= ?")
        params.append(horizon)
    elif watch == "maintenance":
        clauses.append("a.status != 'Retired' AND a.next_maintenance_at != '' AND a.next_maintenance_at <= ?")
        params.append(horizon)
    elif watch == "unassigned":
        clauses.append("a.assigned_to='Unassigned' AND a.status != 'Retired'")
    elif watch == "repair":
        clauses.append("a.status='In repair'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.execute(
        f"""SELECT a.* FROM assets a{where}
        ORDER BY CASE WHEN a.status='Retired' THEN 1 ELSE 0 END,
        CASE WHEN a.next_maintenance_at != '' AND a.next_maintenance_at <= ? THEN 0 ELSE 1 END,
        a.id DESC""",
        (*params, today),
    ).fetchall()
    assets = [decorate_asset(row) for row in rows]
    counts = {state: db.execute("SELECT COUNT(*) FROM assets WHERE status=?", (state,)).fetchone()[0] for state in ASSET_STATUSES}
    metrics = asset_lifecycle_metrics(db)
    attention = db.execute(
        """SELECT a.id, a.asset_tag, a.name, a.warranty_until, a.next_maintenance_at,
        CASE WHEN a.next_maintenance_at != '' AND a.next_maintenance_at <= ?
             THEN 'Maintenance due'
             WHEN a.warranty_until != '' AND a.warranty_until < ?
             THEN 'Warranty expired'
             ELSE 'Warranty expiring' END AS reason
        FROM assets a
        WHERE a.status != 'Retired' AND (
            (a.next_maintenance_at != '' AND a.next_maintenance_at <= ?)
            OR (a.warranty_until != '' AND a.warranty_until <= ?)
        )
        ORDER BY CASE WHEN a.next_maintenance_at != '' AND a.next_maintenance_at <= ? THEN 0 ELSE 1 END,
        COALESCE(NULLIF(a.next_maintenance_at,''), a.warranty_until) ASC LIMIT 6""",
        (today, today, today, horizon, today),
    ).fetchall()
    return render_template(
        "assets.html", active_page="assets", assets=assets, counts=counts,
        metrics=metrics, attention=attention, q=q, watch=watch,
    )


@app.route("/export/assets.csv")
@roles_required("Admin", "IT Agent")
def export_assets():
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.asset_tag, a.name, a.asset_type, a.serial_number, a.vendor,
        a.assigned_to, a.department, a.location, a.status, a.purchase_date, a.purchase_cost,
        a.warranty_until, a.next_maintenance_at, a.maintenance_interval_days, a.updated_at,
        (SELECT COUNT(*) FROM asset_events e WHERE e.asset_id=a.id) AS event_count
        FROM assets a ORDER BY a.asset_tag"""
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Asset ID", "Asset tag", "Name", "Type", "Serial number", "Vendor",
        "Assigned to", "Department", "Location", "Status", "Purchase date",
        "Purchase cost (EUR)", "Warranty end", "Next maintenance",
        "Maintenance interval (days)", "Updated at", "Lifecycle events",
    ])
    for row in rows:
        writer.writerow(tuple(row))
    write_audit_event(
        db, "report", "asset-inventory", "Asset inventory exported",
        f"Asset lifecycle register exported with {len(rows)} device(s).",
        g.user["id"], g.user["full_name"], {"asset_count": len(rows)},
    )
    db.commit()
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=pulsedesk-assets-{datetime.now():%Y-%m-%d}.csv"
    return response


@app.route("/api/assets/lifecycle")
@roles_required("Admin", "IT Agent")
def asset_lifecycle_api():
    db = get_db()
    metrics = asset_lifecycle_metrics(db)
    return jsonify({
        "checked_at": now_iso(),
        "metrics": metrics,
        "attention_count": (
            metrics["warranty_expiring"] + metrics["warranty_expired"]
            + metrics["maintenance_due"]
        ),
    })


@app.route("/assets/<int:asset_id>", methods=["GET", "POST"])
@roles_required("Admin", "IT Agent")
def asset_detail(asset_id: int):
    db = get_db()
    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if asset is None:
        abort(404)
    if request.method == "POST":
        status = request.form.get("status", asset["status"])
        assigned = request.form.get("assigned_to", asset["assigned_to"]).strip()[:100] or "Unassigned"
        department = request.form.get("department", asset["department"]).strip()[:60] or "Operations"
        name = request.form.get("name", asset["name"]).strip()[:100]
        asset_type = request.form.get("asset_type", asset["asset_type"]).strip()[:40]
        serial = request.form.get("serial_number", "").strip()[:100]
        vendor = request.form.get("vendor", "").strip()[:100]
        purchase_date = request.form.get("purchase_date", "").strip()
        warranty = request.form.get("warranty_until", "").strip()
        location = request.form.get("location", "").strip()[:120]
        notes = request.form.get("notes", "").strip()[:2000]
        next_maintenance = request.form.get("next_maintenance_at", "").strip()
        try:
            cost_text = request.form.get("purchase_cost", "").strip()
            purchase_cost = float(cost_text) if cost_text else None
            interval = int(request.form.get("maintenance_interval_days", "180"))
            for label, value in (("Purchase date", purchase_date), ("Warranty end", warranty), ("Next maintenance", next_maintenance)):
                if value:
                    datetime.fromisoformat(value)
        except (TypeError, ValueError):
            flash("Enter valid dates, a numeric purchase cost and a whole-number maintenance interval.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        if not name or status not in ASSET_STATUSES or purchase_cost is not None and purchase_cost < 0 or not 1 <= interval <= 3650:
            flash("Check the asset details, purchase cost and maintenance interval.", "error")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        new_values = {
            "name": name, "asset_type": asset_type, "assigned_to": assigned,
            "department": department, "status": status, "serial_number": serial,
            "vendor": vendor, "purchase_date": purchase_date, "purchase_cost": purchase_cost,
            "warranty_until": warranty, "location": location, "notes": notes,
            "next_maintenance_at": next_maintenance, "maintenance_interval_days": interval,
        }
        changes = {
            field: {"from": asset[field], "to": value}
            for field, value in new_values.items()
            if asset[field] != value
        }
        db.execute(
            """UPDATE assets SET name=?, asset_type=?, assigned_to=?, department=?, status=?,
            serial_number=?, vendor=?, purchase_date=?, purchase_cost=?, warranty_until=?,
            location=?, notes=?, next_maintenance_at=?, maintenance_interval_days=?, updated_at=?
            WHERE id=?""",
            (name, asset_type, assigned, department, status, serial, vendor, purchase_date,
             purchase_cost, warranty, location, notes, next_maintenance, interval, now_iso(), asset_id),
        )
        if changes:
            fragments = []
            for field, value in changes.items():
                label = field.replace("_", " ")
                fragments.append(f"{label}: {value['from'] or '—'} → {value['to'] or '—'}")
            summary = "; ".join(fragments)
            write_asset_event(
                db, asset_id, "Asset details updated", summary,
                g.user["id"], g.user["full_name"], changes,
            )
            log_activity(db, "Asset updated", f"{asset['asset_tag']} · {summary[:140]}", g.user["full_name"])
        db.commit()
        flash("Asset details saved.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    asset = decorate_asset(asset)
    events = db.execute(
        "SELECT * FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 30", (asset_id,)
    ).fetchall()
    maintenance = db.execute(
        """SELECT m.*, u.full_name AS completed_by_name
        FROM asset_maintenance m LEFT JOIN users u ON u.id=m.completed_by
        WHERE m.asset_id=?
        ORDER BY CASE m.status WHEN 'Scheduled' THEN 0 WHEN 'Completed' THEN 1 ELSE 2 END,
        CASE WHEN m.status='Scheduled' THEN m.due_at END ASC, m.id DESC LIMIT 30""",
        (asset_id,),
    ).fetchall()
    return render_template(
        "asset_detail.html", active_page="assets", asset=asset,
        events=events, maintenance=maintenance,
    )


@app.post("/assets/<int:asset_id>/maintenance")
@roles_required("Admin", "IT Agent")
def schedule_asset_maintenance(asset_id: int):
    db = get_db()
    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if asset is None:
        abort(404)
    if asset["status"] == "Retired":
        flash("Retired assets cannot receive new maintenance schedules.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    task = request.form.get("task", "").strip()[:180]
    due_at = request.form.get("due_at", "").strip()
    try:
        due_date = datetime.fromisoformat(due_at).date()
    except (TypeError, ValueError):
        flash("Choose a valid maintenance due date.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    if not task or due_date < datetime.now().date():
        flash("Add a task and choose today or a future date.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    cursor = db.execute(
        """INSERT INTO asset_maintenance
        (asset_id, task, due_at, status, created_by, created_at)
        VALUES (?, ?, ?, 'Scheduled', ?, ?)""",
        (asset_id, task, due_date.isoformat(), g.user["id"], now_iso()),
    )
    next_due = db.execute(
        "SELECT MIN(due_at) FROM asset_maintenance WHERE asset_id=? AND status='Scheduled'",
        (asset_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE assets SET next_maintenance_at=?, updated_at=? WHERE id=?",
        (next_due or "", now_iso(), asset_id),
    )
    write_asset_event(
        db, asset_id, "Maintenance scheduled",
        f"{task} · due {due_date.isoformat()}",
        g.user["id"], g.user["full_name"],
        {"maintenance_id": cursor.lastrowid, "task": task, "due_at": due_date.isoformat()},
    )
    log_activity(db, "Asset maintenance scheduled", f"{asset['asset_tag']} · {task[:100]}", g.user["full_name"])
    db.commit()
    flash("Maintenance scheduled and added to the asset timeline.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.post("/assets/<int:asset_id>/maintenance/<int:maintenance_id>/complete")
@roles_required("Admin", "IT Agent")
def complete_asset_maintenance(asset_id: int, maintenance_id: int):
    db = get_db()
    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    maintenance = db.execute(
        "SELECT * FROM asset_maintenance WHERE id=? AND asset_id=?",
        (maintenance_id, asset_id),
    ).fetchone()
    if asset is None or maintenance is None:
        abort(404)
    if maintenance["status"] != "Scheduled":
        flash("Only scheduled maintenance can be completed.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    outcome = request.form.get("outcome", "").strip()[:500]
    if not outcome:
        flash("Add a short maintenance outcome before completing the task.", "error")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    completed_at = now_iso()
    db.execute(
        """UPDATE asset_maintenance SET status='Completed', outcome=?,
        completed_by=?, completed_at=? WHERE id=? AND status='Scheduled'""",
        (outcome, g.user["id"], completed_at, maintenance_id),
    )
    next_due = db.execute(
        "SELECT MIN(due_at) FROM asset_maintenance WHERE asset_id=? AND status='Scheduled'",
        (asset_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE assets SET next_maintenance_at=?, updated_at=? WHERE id=?",
        (next_due or "", completed_at, asset_id),
    )
    write_asset_event(
        db, asset_id, "Maintenance completed",
        f"{maintenance['task']} · {outcome}",
        g.user["id"], g.user["full_name"],
        {"maintenance_id": maintenance_id, "task": maintenance["task"],
         "due_at": maintenance["due_at"], "outcome": outcome},
    )
    log_activity(db, "Asset maintenance completed", f"{asset['asset_tag']} · {maintenance['task'][:100]}", g.user["full_name"])
    db.commit()
    flash("Maintenance marked complete. The next scheduled date has been recalculated.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


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


def build_analytics(days: int = 30) -> dict:
    """Build a date-windowed analytics snapshot from the live incident register."""
    if days not in (7, 30, 90, 365):
        days = 30

    now = datetime.now().replace(microsecond=0)
    if days in (7, 30):
        first_day = now.date() - timedelta(days=days - 1)
        bucket_starts = [
            datetime.combine(first_day + timedelta(days=index), datetime.min.time())
            for index in range(days)
        ]
        bucket_kind = "daily"
        labels = [item.strftime("%d %b") for item in bucket_starts]
    elif days == 90:
        current_monday = now.date() - timedelta(days=now.weekday())
        first_day = current_monday - timedelta(weeks=12)
        bucket_starts = [
            datetime.combine(first_day + timedelta(weeks=index), datetime.min.time())
            for index in range(13)
        ]
        bucket_kind = "weekly"
        labels = [item.strftime("%d %b") for item in bucket_starts]
    else:
        current_month = now.year * 12 + now.month - 1
        first_month = current_month - 11
        bucket_starts = []
        for index in range(12):
            month_index = first_month + index
            year, month_zero = divmod(month_index, 12)
            bucket_starts.append(datetime(year, month_zero + 1, 1))
        bucket_kind = "monthly"
        labels = [item.strftime("%b %y") for item in bucket_starts]

    period_start = bucket_starts[0]
    previous_duration = now - period_start
    previous_start = period_start - previous_duration
    tickets = get_db().execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()

    trend = [
        {"label": labels[index], "opened": 0, "resolved": 0}
        for index in range(len(bucket_starts))
    ]
    category_counts = {category: 0 for category in CATEGORIES}
    priority_counts = {priority: 0 for priority in PRIORITIES}
    team = {}
    opened = []
    resolved_in_period = []
    previous_opened = 0
    previous_resolved = 0
    overdue_now = 0

    def parse_timestamp(value: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    def bucket_for(moment: datetime) -> int | None:
        if bucket_kind == "daily":
            index = (moment.date() - bucket_starts[0].date()).days
        elif bucket_kind == "weekly":
            index = (moment.date() - bucket_starts[0].date()).days // 7
        else:
            index = (moment.year * 12 + moment.month - 1) - (
                bucket_starts[0].year * 12 + bucket_starts[0].month - 1
            )
        return index if 0 <= index < len(trend) else None

    for ticket in tickets:
        created_at = parse_timestamp(ticket["created_at"])
        updated_at = parse_timestamp(ticket["updated_at"]) or created_at
        resolved_at = parse_timestamp(ticket["resolved_at"]) if ticket["status"] == "Resolved" else None
        resolved_at = resolved_at or (updated_at if ticket["status"] == "Resolved" else None)
        created_in_period = bool(created_at and period_start <= created_at <= now)
        resolved_in_window = bool(
            resolved_at and period_start <= resolved_at <= now
        )
        created_before_period = bool(
            created_at and previous_start <= created_at < period_start
        )
        resolved_before_period = bool(
            resolved_at and previous_start <= resolved_at < period_start
        )

        ticket_is_overdue = ticket_sla(ticket) == "breached"
        if ticket_is_overdue:
            overdue_now += 1
        if created_in_period:
            opened.append(ticket)
            category_counts[ticket["category"]] = category_counts.get(ticket["category"], 0) + 1
            priority_counts[ticket["priority"]] = priority_counts.get(ticket["priority"], 0) + 1
            index = bucket_for(created_at)
            if index is not None:
                trend[index]["opened"] += 1
        if resolved_in_window:
            resolved_in_period.append(ticket)
            index = bucket_for(resolved_at)
            if index is not None:
                trend[index]["resolved"] += 1
        if created_before_period:
            previous_opened += 1
        if resolved_before_period:
            previous_resolved += 1

        owner = (ticket["assignee"] or "Unassigned").strip() or "Unassigned"
        metrics = team.setdefault(owner, {
            "assignee": owner, "assigned": 0, "active": 0, "resolved": 0,
            "overdue": 0, "resolution_hours_total": 0.0, "resolution_samples": 0,
        })
        if created_in_period:
            metrics["assigned"] += 1
        if ticket["status"] != "Resolved":
            metrics["active"] += 1
        if ticket_is_overdue:
            metrics["overdue"] += 1
        if resolved_in_window:
            metrics["resolved"] += 1
            if created_at and resolved_at and resolved_at >= created_at:
                metrics["resolution_hours_total"] += (resolved_at - created_at).total_seconds() / 3600
                metrics["resolution_samples"] += 1

    total = len(tickets)
    resolved_total = sum(ticket["status"] == "Resolved" for ticket in tickets)
    status_rows = [
        {"status": status, "total": sum(ticket["status"] == status for ticket in tickets)}
        for status in STATUSES
    ]
    category_rows = [
        {"category": category, "total": count}
        for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        if count
    ]
    priority_rows = [
        {"priority": priority, "total": priority_counts[priority]}
        for priority in PRIORITIES
    ]
    team_rows = []
    for metrics in team.values():
        samples = metrics.pop("resolution_samples")
        hours_total = metrics.pop("resolution_hours_total")
        metrics["avg_resolution_hours"] = round(hours_total / samples, 1) if samples else None
        team_rows.append(metrics)
    team_rows.sort(key=lambda item: (-item["active"], -item["resolved"], item["assignee"].lower()))

    current_opened = len(opened)
    current_resolved = len(resolved_in_period)

    def percentage_change(current: int, previous: int) -> float | None:
        if previous == 0:
            return 100.0 if current else 0.0
        return round((current - previous) * 100 / previous, 1)

    return {
        "days": days,
        "period_label": f"{period_start:%d %b %Y} — {now:%d %b %Y}",
        "bucket_kind": bucket_kind,
        "trend": trend,
        "total": total,
        "opened": current_opened,
        "resolved": current_resolved,
        "resolved_total": resolved_total,
        "active": total - resolved_total,
        "overdue": overdue_now,
        "resolution_rate": round(resolved_total * 100 / max(1, total)),
        "sla_compliance": round(((total - resolved_total - overdue_now) * 100) / max(1, total - resolved_total)) if total > resolved_total else 100,
        "category_rows": category_rows,
        "priority_rows": priority_rows,
        "status_rows": status_rows,
        "team_rows": team_rows,
        "changes": {
            "opened": percentage_change(current_opened, previous_opened),
            "resolved": percentage_change(current_resolved, previous_resolved),
        },
    }


@app.route("/assistant", methods=["GET", "POST"])
@roles_required("Admin", "IT Agent")
def smart_assistant():
    db = get_db()
    form_data = {"title": "", "description": "", "category": "Other"}
    analysis = None

    if request.method == "POST":
        form_data["title"] = request.form.get("title", "").strip()[:140]
        form_data["description"] = request.form.get("description", "").strip()[:2000]
        form_data["category"] = request.form.get("category", "Other")
        if not form_data["title"]:
            flash("Add an incident title so Pulse Assist has something to analyse.", "error")
        else:
            analysis = analyse_incident(
                db, form_data["title"], form_data["description"], form_data["category"]
            )
    else:
        ticket_id = request.args.get("ticket_id", type=int)
        if ticket_id:
            ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if ticket:
                form_data = {
                    "title": ticket["title"],
                    "description": ticket["description"],
                    "category": ticket["category"] if ticket["category"] in CATEGORIES else "Other",
                }
                analysis = analyse_incident(
                    db, form_data["title"], form_data["description"], form_data["category"]
                )

    summary = build_daily_operations_summary(db)
    return render_template(
        "assistant.html", active_page="assistant", analysis=analysis,
        form_data=form_data, summary=summary,
    )


@app.post("/api/assistant/analyze")
@roles_required("Admin", "IT Agent")
def assistant_analyze_api():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object describing the incident."}), 400
    title = str(payload.get("title", "")).strip()[:140]
    description = str(payload.get("description", "")).strip()[:2000]
    category = str(payload.get("category", "Other"))
    if not title:
        return jsonify({"error": "An incident title is required."}), 400
    result = analyse_incident(get_db(), title, description, category)
    return jsonify(result)


@app.route("/api/assistant/summary")
@roles_required("Admin", "IT Agent")
def assistant_summary_api():
    return jsonify(build_daily_operations_summary(get_db()))


@app.route("/api/assistant/recurrences")
@roles_required("Admin", "IT Agent")
def assistant_recurrences_api():
    try:
        days = int(request.args.get("days", "90"))
    except (TypeError, ValueError):
        days = 90
    days = min(365, max(7, days))
    tickets = get_db().execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
    return jsonify({"days": days, "patterns": detect_recurring_incidents(tickets, days)})


@app.route("/monitoring")
@roles_required("Admin", "IT Agent")
def monitoring_page():
    db = get_db()
    calendar = get_sla_calendar(db)
    worker = worker_status_snapshot(db)
    worker_last_summary = ""
    if worker.get("last_result"):
        try:
            last_result = json.loads(worker["last_result"])
            worker_last_summary = (
                f"{last_result.get('checked', 0)} incidents checked · "
                f"{last_result.get('escalated_count', 0)} new escalation(s)"
            )
        except (TypeError, ValueError):
            worker_last_summary = "Previous worker result is not available in summary format."
    outbox = db.execute(
        """SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status='preview' THEN 1 ELSE 0 END) AS preview,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM email_outbox"""
    ).fetchone()
    recent_mail = db.execute(
        "SELECT id, recipient, subject, status, error, created_at FROM email_outbox ORDER BY id DESC LIMIT 8"
    ).fetchall()
    return render_template(
        "monitoring.html", active_page="monitoring", calendar=calendar, worker=worker,
        outbox=dict(outbox), recent_mail=recent_mail, smtp_configured=smtp_is_configured(),
        worker_last_summary=worker_last_summary,
    )


@app.post("/monitoring/calendar")
@roles_required("Admin")
def save_monitoring_calendar():
    db = get_db()
    timezone_name = request.form.get("timezone_name", "Europe/Riga").strip()[:80]
    start_value = request.form.get("business_start", "09:00")
    end_value = request.form.get("business_end", "17:00")
    weekdays = sorted({int(value) for value in request.form.getlist("weekdays") if value.isdigit() and 0 <= int(value) <= 6})
    enabled = 1 if request.form.get("scheduler_enabled") == "1" else 0
    try:
        ZoneInfo(timezone_name)
        start = time.fromisoformat(start_value)
        end = time.fromisoformat(end_value)
        interval = int(request.form.get("interval_seconds", "60"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        flash("Enter a valid timezone, working hours and worker interval.", "error")
        return redirect(url_for("monitoring_page"))
    if start >= end or not weekdays or interval not in (30, 60, 120, 300, 600):
        flash("Choose working hours with an end after the start, at least one working day and a supported check interval.", "error")
        return redirect(url_for("monitoring_page"))

    db.execute(
        """UPDATE sla_calendar
        SET timezone_name=?, business_start=?, business_end=?, weekdays=?,
            scheduler_enabled=?, interval_seconds=?, updated_by=?, updated_at=?
        WHERE id=1""",
        (timezone_name, start.strftime("%H:%M"), end.strftime("%H:%M"),
         ",".join(str(day) for day in weekdays), enabled, interval,
         g.user["id"], now_iso()),
    )
    write_audit_event(
        db, "sla_calendar", "1", "Monitoring settings updated",
        f"Business calendar set to {timezone_name}, {start:%H:%M}–{end:%H:%M}, weekdays {weekdays}; automatic checks {'enabled' if enabled else 'paused'}.",
        g.user["id"], g.user["full_name"],
        {"timezone": timezone_name, "business_start": start.strftime("%H:%M"),
         "business_end": end.strftime("%H:%M"), "weekdays": weekdays,
         "scheduler_enabled": bool(enabled), "interval_seconds": interval},
    )
    db.commit()
    flash("Monitoring settings saved. The running worker will pick them up on its next heartbeat.", "success")
    return redirect(url_for("monitoring_page"))


@app.post("/monitoring/test-email")
@roles_required("Admin")
def send_monitoring_test_email():
    db = get_db()
    subject = "PulseDesk SMTP connectivity test"
    body = (
        f"Hello {g.user['full_name']},\n\n"
        "This is a test message generated from the PulseDesk Monitoring & Integrations screen.\n"
        f"Requested at: {now_iso()}\n"
        f"SMTP configured: {'yes' if smtp_is_configured() else 'no'}\n\n"
        "If SMTP is not configured, PulseDesk stores a preview in the local outbox instead of sending a message."
    )
    status = deliver_email(db, g.user["email"], subject, body)
    write_audit_event(
        db, "integration", "smtp", "SMTP test requested",
        f"SMTP test for {g.user['email']} completed with status: {status}.",
        g.user["id"], g.user["full_name"], {"status": status},
    )
    db.commit()
    if status == "sent":
        flash("Test email sent. Check your inbox.", "success")
    elif status == "preview":
        flash("SMTP is not configured. A clearly labelled preview was saved in the email outbox; no email was sent.", "error")
    else:
        flash("SMTP delivery failed. Review the error shown in the outbox.", "error")
    return redirect(url_for("monitoring_page"))


@app.route("/api/monitoring/health")
@roles_required("Admin", "IT Agent")
def monitoring_health_api():
    db = get_db()
    calendar = get_sla_calendar(db)
    worker = worker_status_snapshot(db)
    try:
        db.execute("SELECT 1").fetchone()
        quick_check = db.execute("PRAGMA quick_check").fetchone()
        database_ok = bool(quick_check and quick_check[0] == "ok")
    except sqlite3.DatabaseError:
        database_ok = False
    outbox = db.execute(
        """SELECT COUNT(*) AS total,
        SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
        SUM(CASE WHEN status='preview' THEN 1 ELSE 0 END) AS preview,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM email_outbox"""
    ).fetchone()
    return jsonify({
        "checked_at": now_iso(),
        "database": {"status": "healthy" if database_ok else "error", "ok": database_ok},
        "worker": {
            "status": worker["status"], "online": worker["online"],
            "heartbeat_age_seconds": worker["heartbeat_age_seconds"],
            "last_heartbeat": worker["last_heartbeat"], "last_run": worker["last_run"],
            "last_result": worker["last_result"], "last_error": worker["last_error"],
        },
        "scheduler": {
            "enabled": bool(calendar["scheduler_enabled"]),
            "interval_seconds": calendar["interval_seconds"],
            "timezone": calendar["timezone_name"],
        },
        "smtp": {"configured": smtp_is_configured(), "outbox": dict(outbox)},
    })


@app.route("/workflow")
@roles_required("Admin", "IT Agent")
def workflow_page():
    db = get_db()
    rules = db.execute(
        """SELECT * FROM workflow_rules
        ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END,
        escalation_level"""
    ).fetchall()
    audit = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 12").fetchall()
    active_tickets = db.execute(
        "SELECT * FROM tickets WHERE status != 'Resolved' ORDER BY due_at ASC"
    ).fetchall()
    overdue_count = sum(ticket_sla(ticket) == "breached" for ticket in active_tickets)
    return render_template(
        "workflow.html", active_page="workflow", rules=rules, recent_audit=audit,
        active_count=len(active_tickets), overdue_count=overdue_count,
    )


@app.post("/workflow/rules/save")
@roles_required("Admin")
def save_workflow_rule():
    db = get_db()
    rule_id = request.form.get("rule_id", type=int)
    priority = request.form.get("priority", "")
    level = request.form.get("escalation_level", type=int)
    overdue_minutes = request.form.get("overdue_minutes", type=int)
    is_active = 1 if request.form.get("is_active", "1") == "1" else 0
    if priority not in PRIORITIES or level not in ESCALATION_TARGETS or overdue_minutes is None or not 0 <= overdue_minutes <= 43200:
        flash("Choose a valid priority, escalation level and delay between 0 and 43,200 minutes.", "error")
        return redirect(url_for("workflow_page"))
    target = ESCALATION_TARGETS[level]
    try:
        if rule_id:
            existing = db.execute("SELECT id FROM workflow_rules WHERE id = ?", (rule_id,)).fetchone()
            if not existing:
                abort(404)
            db.execute(
                """UPDATE workflow_rules
                SET priority=?, escalation_level=?, overdue_minutes=?, target_role=?,
                    is_active=?, updated_by=?, updated_at=? WHERE id=?""",
                (priority, level, overdue_minutes, target, is_active, g.user["id"], now_iso(), rule_id),
            )
        else:
            db.execute(
                """INSERT INTO workflow_rules
                (priority, escalation_level, overdue_minutes, target_role, is_active, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (priority, level, overdue_minutes, target, is_active, g.user["id"], now_iso()),
            )
        write_audit_event(
            db, "workflow_rule", rule_id or f"{priority}-{level}", "Rule saved",
            f"{priority} incidents escalate to {target} at {overdue_minutes} minutes past SLA.",
            g.user["id"], g.user["full_name"],
            {"priority": priority, "level": level, "overdue_minutes": overdue_minutes,
             "target_role": target, "is_active": bool(is_active)},
        )
        log_activity(db, "SLA workflow rule saved", f"{priority} → Level {level} → {overdue_minutes} min", g.user["full_name"])
        db.commit()
        flash("Workflow rule saved.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("A rule for that priority and escalation level already exists. Edit the existing row instead.", "error")
    return redirect(url_for("workflow_page"))


@app.post("/workflow/rules/<int:rule_id>/toggle")
@roles_required("Admin")
def toggle_workflow_rule(rule_id: int):
    db = get_db()
    rule = db.execute("SELECT * FROM workflow_rules WHERE id = ?", (rule_id,)).fetchone()
    if rule is None:
        abort(404)
    active = 0 if rule["is_active"] else 1
    db.execute(
        "UPDATE workflow_rules SET is_active=?, updated_by=?, updated_at=? WHERE id=?",
        (active, g.user["id"], now_iso(), rule_id),
    )
    write_audit_event(
        db, "workflow_rule", rule_id, "Rule enabled" if active else "Rule disabled",
        f"{rule['priority']} level {rule['escalation_level']} rule {'enabled' if active else 'disabled'}.",
        g.user["id"], g.user["full_name"],
        {"priority": rule["priority"], "level": rule["escalation_level"], "is_active": bool(active)},
    )
    db.commit()
    flash("Workflow rule " + ("enabled." if active else "paused."), "success")
    return redirect(url_for("workflow_page"))


@app.post("/workflow/run-sla")
@roles_required("Admin", "IT Agent")
def run_sla_workflow():
    db = get_db()
    result = run_sla_escalation_check(db, g.user["id"], g.user["full_name"])
    write_audit_event(
        db, "workflow_run", "sla-escalation", "SLA check completed",
        f"Checked {result['checked']} active incidents; escalated {result['escalated_count']}.",
        g.user["id"], g.user["full_name"], result,
    )
    db.commit()
    if result["escalated_count"]:
        flash(f"SLA check complete: {result['escalated_count']} incident(s) escalated.", "success")
    else:
        flash(f"SLA check complete: {result['checked']} active incident(s) checked; no new escalation stages were due.", "success")
    return redirect(url_for("workflow_page"))


@app.route("/audit-log")
@roles_required("Admin", "IT Agent")
def audit_log_page():
    q = request.args.get("q", "").strip()[:100]
    if q:
        pattern = f"%{q}%"
        rows = get_db().execute(
            """SELECT * FROM audit_events
            WHERE actor_name LIKE ? OR action LIKE ? OR summary LIKE ? OR entity_id LIKE ?
            ORDER BY id DESC LIMIT 250""",
            (pattern, pattern, pattern, pattern),
        ).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT 250"
        ).fetchall()
    return render_template("audit_log.html", active_page="audit", events=rows, q=q)


@app.route("/export/daily-operations.csv")
@roles_required("Admin", "IT Agent")
def export_daily_operations():
    db = get_db()
    summary = build_daily_operations_summary(db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["PulseDesk daily operations brief", summary["date_label"]])
    writer.writerow([])
    writer.writerow(["Metric", "Value"])
    for label, key in (
        ("All incidents", "total"), ("Created today", "created_today"),
        ("Resolved today", "resolved_today"), ("Active queue", "active"),
        ("Critical active", "critical"), ("Overdue active", "overdue"),
        ("Unassigned active", "unassigned"), ("Largest active category", "top_category"),
    ):
        writer.writerow([label, summary[key]])
    writer.writerow([])
    writer.writerow(["Attention points"])
    for item in summary["highlights"]:
        writer.writerow([item])
    writer.writerow([])
    writer.writerow(["Recurring themes", "Matches", "Open", "Critical"])
    for pattern in summary["patterns"]:
        writer.writerow([pattern["theme"], pattern["count"], pattern["open_count"], pattern["critical_count"]])
    writer.writerow([])
    writer.writerow(["Open incident register", "Priority", "Status", "Assignee", "Due"])
    active = db.execute(
        "SELECT id, title, priority, status, assignee, due_at FROM tickets WHERE status != 'Resolved' ORDER BY due_at ASC"
    ).fetchall()
    for ticket in active:
        writer.writerow([f"#{ticket['id']} {ticket['title']}", ticket["priority"], ticket["status"], ticket["assignee"], ticket["due_at"]])
    write_audit_event(
        db, "report", "daily-operations", "Daily report exported",
        f"Daily operations CSV exported for {summary['date_label']}.",
        g.user["id"], g.user["full_name"],
        {"active": summary["active"], "overdue": summary["overdue"]},
    )
    db.commit()
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=pulsedesk-daily-operations-{datetime.now():%Y-%m-%d}.csv"
    return response


@app.post("/tickets/<int:ticket_id>/assign")
@roles_required("Admin", "IT Agent")
def assign_ticket(ticket_id: int):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        abort(404)
    if ticket["status"] == "Resolved":
        flash("Resolved incidents cannot be reassigned.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    assignee = request.form.get("assignee", "").strip()
    candidate = db.execute(
        "SELECT id, full_name FROM users WHERE full_name = ? AND active = 1 AND role IN ('Admin','IT Agent')",
        (assignee,),
    ).fetchone()
    if not candidate:
        flash("Choose an active Admin or IT Agent account.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    previous = ticket["assignee"]
    db.execute(
        "UPDATE tickets SET assignee=?, updated_at=? WHERE id=?",
        (candidate["full_name"], now_iso(), ticket_id),
    )
    log_activity(db, "Ticket assigned", f"#{ticket_id} → {candidate['full_name']}", g.user["full_name"])
    write_audit_event(
        db, "ticket", ticket_id, "Assignee changed",
        f"Incident #{ticket_id} reassigned from {previous} to {candidate['full_name']}.",
        g.user["id"], g.user["full_name"],
        {"previous_assignee": previous, "new_assignee": candidate["full_name"]},
    )
    create_notification(
        db, candidate["id"], "assignment", f"Incident #{ticket_id} assigned",
        f"{g.user['full_name']} assigned you: {ticket['title']}",
        f"/tickets/{ticket_id}",
    )
    notify_ticket_owner(
        db, ticket, "assignment", f"Request #{ticket_id} assigned",
        f"Your request is now assigned to {candidate['full_name']}.",
    )
    db.commit()
    flash(f"Incident #{ticket_id} assigned to {candidate['full_name']}.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/api/tickets/<int:ticket_id>/assignment-suggestions")
@roles_required("Admin", "IT Agent")
def assignment_suggestions_api(ticket_id: int):
    ticket = get_db().execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        abort(404)
    return jsonify({"ticket_id": ticket_id, "suggestions": suggest_ticket_assignees(get_db(), ticket)})


@app.route("/reports")
@roles_required("Admin", "IT Agent")
def reports_page():
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90, 365):
        days = 30
    analytics = build_analytics(days)
    recent = get_db().execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 8").fetchall()
    return render_template(
        "reports.html", active_page="reports", analytics=analytics, recent=recent
    )


@app.route("/api/analytics")
@roles_required("Admin", "IT Agent")
def analytics_api():
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    return jsonify(build_analytics(days))


@app.route("/export/analytics.csv")
@roles_required("Admin", "IT Agent")
def export_analytics():
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    analytics = build_analytics(days)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["PulseDesk analytics", analytics["period_label"]])
    writer.writerow([])
    writer.writerow(["Team member", "Incidents assigned in period", "Active workload", "Resolved in period", "Overdue active", "Average resolution hours"])
    for row in analytics["team_rows"]:
        writer.writerow([
            row["assignee"], row["assigned"], row["active"], row["resolved"],
            row["overdue"], row["avg_resolution_hours"] if row["avg_resolution_hours"] is not None else "",
        ])
    writer.writerow([])
    writer.writerow(["Bucket", "Opened", "Resolved"])
    for row in analytics["trend"]:
        writer.writerow([row["label"], row["opened"], row["resolved"]])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=pulsedesk-analytics-{days}d.csv"
    return response


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
