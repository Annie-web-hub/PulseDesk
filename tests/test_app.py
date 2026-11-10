import csv
import gc
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import app, init_db


class PulseDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-pulsedesk.db"
        app.config.update(TESTING=True, DATABASE=str(self.db_path))
        init_db()
        self.client = self.login_client("admin@pulsedesk.demo", "AdminDemo!2026")

    def tearDown(self):
        self.client = None
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def csrf_for(client, path="/login"):
        response = client.get(path)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError(f"No CSRF token found on {path}")
        return match.group(1).decode()

    @classmethod
    def login_client(cls, email, password):
        client = app.test_client()
        token = cls.csrf_for(client, "/login")
        response = client.post(
            "/login",
            data={"email": email, "password": password, "csrf_token": token},
            follow_redirects=False,
        )
        if password and response.status_code not in (302, 303):
            raise AssertionError(f"Login failed for {email}: HTTP {response.status_code}")
        return client

    def post(self, path, data=None, client=None, **kwargs):
        client = client or self.client
        payload = dict(data or {})
        with client.session_transaction() as session:
            payload["csrf_token"] = session["_csrf_token"]
        return client.post(path, data=payload, **kwargs)

    def test_dashboard_loads_seeded_workspace(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Make the signal", response.data)
        self.assertIn(b"Incidents in motion", response.data)

    def test_login_page_and_invalid_password(self):
        client = app.test_client()
        token = self.csrf_for(client, "/login")
        response = client.post("/login", data={
            "email": "admin@pulsedesk.demo",
            "password": "not-the-password",
            "csrf_token": token,
        })
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"not recognised", response.data)

    def test_protected_pages_redirect_to_login(self):
        client = app.test_client()
        response = client.get("/tickets")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_csrf_is_required_for_post_requests(self):
        response = self.client.post("/tickets", data={
            "title": "Missing token request",
            "requester": "Alex Morgan",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"form session expired", response.data)

    def test_roles_restrict_sensitive_pages(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        for path in ("/tickets", "/assets", "/reports", "/export/tickets.csv", "/api/metrics", "/admin/users"):
            with self.subTest(path=path):
                self.assertEqual(employee.get(path).status_code, 403)
        self.assertEqual(employee.get("/knowledge").status_code, 200)
        self.assertEqual(employee.get("/").status_code, 302)

        agent = self.login_client("agent@pulsedesk.demo", "AgentDemo!2026")
        self.assertEqual(agent.get("/tickets").status_code, 200)
        self.assertEqual(agent.get("/assets").status_code, 200)
        self.assertEqual(agent.get("/admin/users").status_code, 403)

    def test_employee_can_only_view_their_own_requests(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        response = self.post("/my-tickets", {
            "title": "My laptop camera is not working",
            "description": "The camera is not detected in meetings.",
            "department": "Operations",
            "category": "Hardware",
        }, client=employee, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"My laptop camera is not working", response.data)
        with sqlite3.connect(self.db_path) as db:
            own_id = db.execute(
                "SELECT id FROM tickets WHERE title = ?", ("My laptop camera is not working",)
            ).fetchone()[0]
        self.assertEqual(employee.get(f"/tickets/{own_id}").status_code, 200)
        self.assertEqual(employee.get("/tickets/1").status_code, 404)
        self.assertEqual(employee.post(f"/tickets/{own_id}", data={}).status_code, 400)

    def test_admin_can_create_user_account(self):
        response = self.post("/admin/users", {
            "email": "new.agent@pulsedesk.demo",
            "full_name": "Taylor Reed",
            "role": "IT Agent",
            "password": "TemporaryPass!2026",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Taylor Reed", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT role, password_hash FROM users WHERE email = ?",
                ("new.agent@pulsedesk.demo",),
            ).fetchone()
        self.assertEqual(row[0], "IT Agent")
        self.assertNotEqual(row[1], "TemporaryPass!2026")

    def test_employee_request_notifies_it_and_records_email_previews(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        with patch.dict(os.environ, {"PULSEDESK_SMTP_HOST": "", "PULSEDESK_SMTP_FROM": ""}):
            response = self.post("/my-tickets", {
                "title": "Payroll portal is unavailable",
                "description": "The team cannot open the payroll portal.",
                "department": "Finance",
                "category": "Software",
            }, client=employee, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            notification_count = db.execute(
                "SELECT COUNT(*) FROM notifications WHERE kind = 'ticket'"
            ).fetchone()[0]
            preview_count = db.execute(
                "SELECT COUNT(*) FROM email_outbox WHERE status = 'preview'"
            ).fetchone()[0]
            recipients = {
                row[0] for row in db.execute(
                    "SELECT recipient FROM email_outbox WHERE status = 'preview'"
                ).fetchall()
            }
        self.assertEqual(notification_count, 2)
        self.assertEqual(preview_count, 2)
        self.assertEqual(recipients, {"admin@pulsedesk.demo", "agent@pulsedesk.demo"})

    def test_notification_centre_marks_only_the_current_users_alert_read(self):
        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Signal, not noise.", response.data)
        with sqlite3.connect(self.db_path) as db:
            admin_id = db.execute(
                "SELECT id FROM users WHERE email = 'admin@pulsedesk.demo'"
            ).fetchone()[0]
            employee_id = db.execute(
                "SELECT id FROM users WHERE email = 'employee@pulsedesk.demo'"
            ).fetchone()[0]
            admin_alert = db.execute(
                "SELECT id FROM notifications WHERE user_id = ? ORDER BY id LIMIT 1",
                (admin_id,),
            ).fetchone()[0]
        response = self.post(
            f"/notifications/{admin_alert}/read",
            {"next": "/notifications"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            admin_read = db.execute(
                "SELECT is_read FROM notifications WHERE id = ?", (admin_alert,)
            ).fetchone()[0]
            employee_unread = db.execute(
                "SELECT is_read FROM notifications WHERE user_id = ? ORDER BY id LIMIT 1",
                (employee_id,),
            ).fetchone()[0]
        self.assertEqual(admin_read, 1)
        self.assertEqual(employee_unread, 0)

    def test_ticket_status_change_notifies_employee_requester(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.post("/my-tickets", {
            "title": "Access to reporting workspace",
            "description": "Please restore access to the quarterly workspace.",
            "department": "Sales",
            "category": "Access",
        }, client=employee, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            ticket_id = db.execute(
                "SELECT id FROM tickets WHERE title = ?",
                ("Access to reporting workspace",),
            ).fetchone()[0]
            employee_id = db.execute(
                "SELECT id FROM users WHERE email = 'employee@pulsedesk.demo'"
            ).fetchone()[0]
        response = self.post(f"/tickets/{ticket_id}", {
            "status": "In Progress",
            "priority": "Medium",
            "assignee": "Jordan Lee",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            notification = db.execute(
                "SELECT title, message FROM notifications WHERE user_id = ? AND kind = 'status' ORDER BY id DESC LIMIT 1",
                (employee_id,),
            ).fetchone()
            email_status = db.execute(
                "SELECT status FROM email_outbox WHERE recipient = ? ORDER BY id DESC LIMIT 1",
                ("employee@pulsedesk.demo",),
            ).fetchone()[0]
        self.assertIn(str(ticket_id), notification[0])
        self.assertIn("In Progress", notification[1])
        self.assertEqual(email_status, "preview")

    def test_live_updates_api_returns_new_activity_and_current_metrics(self):
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
            db.execute(
                "INSERT INTO activity (action, detail, actor, created_at) VALUES (?, ?, ?, ?)",
                ("Ticket updated", "#0042 - status: In Progress", "Jordan Lee", datetime.now().isoformat()),
            )
            db.commit()
        response = self.client.get(f"/api/updates?after_id={cursor}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["activity"][0]["action"], "Ticket updated")
        self.assertEqual(payload["activity"][0]["actor"], "Jordan Lee")
        self.assertEqual(payload["metrics"]["total"], 12)
        self.assertIn("unread", payload)
        self.assertIn("notifications", payload)
        self.assertGreaterEqual(len(payload["notifications"]), 1)
        self.assertGreater(payload["cursor"], cursor)

    def test_smtp_delivery_sends_message_when_configured(self):
        settings = {
            "PULSEDESK_SMTP_HOST": "smtp.example.test",
            "PULSEDESK_SMTP_PORT": "587",
            "PULSEDESK_SMTP_FROM": "alerts@example.test",
            "PULSEDESK_SMTP_USERNAME": "alerts",
            "PULSEDESK_SMTP_PASSWORD": "test-placeholder",
            "PULSEDESK_SMTP_STARTTLS": "1",
        }
        with patch.dict(os.environ, settings):
            with patch.object(app_module.smtplib, "SMTP") as smtp_factory:
                smtp_client = smtp_factory.return_value.__enter__.return_value
                with app.app_context():
                    db = app_module.get_db()
                    status = app_module.deliver_email(
                        db, "operator@example.test", "PulseDesk test", "Test alert body."
                    )
                    db.commit()
                smtp_client.starttls.assert_called_once()
                smtp_client.login.assert_called_once_with("alerts", "test-placeholder")
                smtp_client.send_message.assert_called_once()
        self.assertEqual(status, "sent")
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, recipient FROM email_outbox ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, ("sent", "operator@example.test"))

    def test_sla_watchlist_only_returns_unresolved_overdue_tickets(self):
        response = self.client.get("/tickets?sla=breached")
        self.assertEqual(response.status_code, 200)
        ids = [int(value) for value in re.findall(rb'data-href="/tickets/([0-9]+)"', response.data)]
        self.assertGreater(len(ids), 0)
        self.assertIn(b'name="sla" value="breached"', response.data)
        with sqlite3.connect(self.db_path) as connection:
            for ticket_id in ids:
                status, due_at = connection.execute(
                    "SELECT status, due_at FROM tickets WHERE id = ?", (ticket_id,)
                ).fetchone()
                self.assertNotEqual(status, "Resolved")
                self.assertLess(datetime.fromisoformat(due_at), datetime.now())

    def test_create_incident_and_smart_triage(self):
        response = self.post("/tickets", {
            "title": "Production blocked after server outage",
            "description": "The server is down and all users are affected.",
            "requester": "Taylor Reed",
            "department": "Engineering",
            "category": "Network",
            "assignee": "Jordan Lee",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Critical priority", response.data)

    def test_ticket_filters_by_priority_and_search(self):
        response = self.client.get("/tickets?priority=Critical&q=finance")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Shared finance drive unavailable", response.data)
        self.assertNotIn(b"Adobe licence renewal", response.data)

    def test_ticket_status_and_assignee_can_be_updated(self):
        response = self.post("/tickets/1", {
            "status": "Resolved", "priority": "Medium", "assignee": "Maya Support",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Resolved", response.data)
        self.assertIn(b"Maya Support", response.data)
        with sqlite3.connect(self.db_path) as db:
            first_resolved_at = db.execute(
                "SELECT resolved_at FROM tickets WHERE id = 1"
            ).fetchone()[0]
        self.assertTrue(first_resolved_at)

        # A later priority edit must not move the actual resolution timestamp.
        self.post("/tickets/1", {
            "status": "Resolved", "priority": "Low", "assignee": "Maya Support",
        }, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            unchanged_resolved_at = db.execute(
                "SELECT resolved_at FROM tickets WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual(unchanged_resolved_at, first_resolved_at)

        # Reopening clears the resolution timestamp so the next closure is measured afresh.
        self.post("/tickets/1", {
            "status": "In Progress", "priority": "Low", "assignee": "Maya Support",
        }, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            reopened = db.execute(
                "SELECT resolved_at FROM tickets WHERE id = 1"
            ).fetchone()[0]
        self.assertIsNone(reopened)

    def test_legacy_resolved_tickets_receive_a_resolution_timestamp(self):
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status = 'Resolved' AND resolved_at IS NOT NULL"
            ).fetchone()[0]
            total = db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status = 'Resolved'"
            ).fetchone()[0]
        self.assertEqual(rows, total)

    def test_invalid_ticket_workflow_values_are_rejected(self):
        response = self.post("/tickets/1", {
            "status": "Delete everything", "priority": "Impossible", "assignee": "Someone",
        })
        self.assertEqual(response.status_code, 400)

    def test_escalation_creates_auditable_history(self):
        response = self.post("/tickets/1/escalate", {
            "reason": "Multiple finance users remain blocked and the shared drive outage is unresolved.",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Escalated to Service Lead", response.data)
        with sqlite3.connect(self.db_path) as db:
            level = db.execute("SELECT escalation_level FROM tickets WHERE id = 1").fetchone()[0]
            history = db.execute(
                "SELECT level, target_role, reason, actor_name FROM ticket_escalations WHERE ticket_id = 1"
            ).fetchone()
        self.assertEqual(level, 1)
        self.assertEqual(history[0], 1)
        self.assertEqual(history[1], "Service Lead")
        self.assertIn("finance users", history[2])
        self.assertEqual(history[3], "Alex Morgan")

    def test_employee_cannot_escalate_an_incident(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        response = self.post("/tickets/1/escalate", {}, client=employee)
        self.assertEqual(response.status_code, 403)

    def test_escalation_requires_a_reason_and_respects_level_limit(self):
        response = self.post("/tickets/1/escalate", {"reason": "No"})
        self.assertEqual(response.status_code, 302)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT escalation_level FROM tickets WHERE id=1").fetchone()[0], 0)
            db.execute("UPDATE tickets SET escalation_level=3 WHERE id=1")
            db.commit()
        response = self.post("/tickets/1/escalate", {
            "reason": "Continue to the next level after the last escalation."
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ticket_escalations WHERE ticket_id=1").fetchone()[0], 0)

    def test_resolved_incident_cannot_be_escalated(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE tickets SET status='Resolved' WHERE id=1")
            db.commit()
        response = self.post("/tickets/1/escalate", {
            "reason": "This should not be accepted for a resolved incident."
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ticket_escalations").fetchone()[0], 0)

    def test_asset_can_be_registered(self):
        response = self.post("/assets", {
            "asset_tag": "AST-999", "name": "Test laptop", "asset_type": "Laptop",
            "assigned_to": "Taylor Reed", "department": "Engineering",
            "status": "In use", "warranty_until": "2028-01-01",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AST-999", response.data)
        self.assertIn(b"Test laptop", response.data)

    def test_duplicate_asset_tag_is_handled(self):
        self.post("/assets", {
            "asset_tag": "AST-001", "name": "Duplicate device", "status": "In stock",
        }, follow_redirects=True)
        response = self.client.get("/assets")
        self.assertIn(b"MacBook Pro 14", response.data)
        self.assertNotIn(b"Duplicate device", response.data)

    def test_knowledge_article_renders_and_tracks_views(self):
        response = self.client.get("/knowledge/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reset your multi-factor authentication", response.data)
        self.assertIn(b"Before you begin", response.data)

    def test_knowledge_search_filters_articles(self):
        response = self.client.get("/knowledge?q=VPN")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Connect to the company VPN", response.data)
        self.assertNotIn(b"Prepare a laptop", response.data)

    def test_analytics_page_renders_period_filter_charts_and_team_table(self):
        response = self.client.get("/reports?days=7")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Know where the work goes.", response.data)
        self.assertIn(b'id="incident-trend-chart"', response.data)
        self.assertIn(b'id="priority-donut-chart"', response.data)
        self.assertIn(b'id="team-capacity-chart"', response.data)
        self.assertIn(b"Ownership & delivery", response.data)
        self.assertIn(b'value="7" selected', response.data)
        self.assertIn(b"analytics.js", response.data)

    def test_analytics_api_returns_real_bucketed_and_team_metrics(self):
        with sqlite3.connect(self.db_path) as db:
            now = datetime.now().replace(microsecond=0)
            created = now.isoformat(timespec="seconds")
            due = (now + timedelta(days=2)).isoformat(timespec="seconds")
            db.execute(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Analytics test incident", "Created for analytics coverage.", "Test User",
                 "Engineering", "Software", "High", "Resolved", "Jordan Lee",
                 created, created, due),
            )
            db.commit()
        response = self.client.get("/api/analytics?days=7")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["days"], 7)
        self.assertEqual(payload["bucket_kind"], "daily")
        expected_sla = round((payload["active"] - payload["overdue"]) * 100 / max(1, payload["active"])) if payload["active"] else 100
        self.assertEqual(payload["sla_compliance"], expected_sla)
        self.assertEqual(len(payload["trend"]), 7)
        self.assertEqual(sum(row["opened"] for row in payload["trend"]), payload["opened"])
        self.assertEqual(sum(row["resolved"] for row in payload["trend"]), payload["resolved"])
        jordan = next(row for row in payload["team_rows"] if row["assignee"] == "Jordan Lee")
        self.assertGreaterEqual(jordan["assigned"], 1)
        self.assertGreaterEqual(jordan["resolved"], 1)
        self.assertIsNotNone(jordan["avg_resolution_hours"])
        self.assertIn("Unassigned", {row["assignee"] for row in payload["team_rows"]})

    def test_analytics_periods_are_validated_and_csv_export_is_scoped(self):
        defaulted = self.client.get("/api/analytics?days=999")
        self.assertEqual(defaulted.status_code, 200)
        self.assertEqual(defaulted.get_json()["days"], 30)
        invalid = self.client.get("/api/analytics?days=not-a-number")
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.get_json()["days"], 30)
        monthly = self.client.get("/api/analytics?days=365")
        self.assertEqual(monthly.get_json()["bucket_kind"], "monthly")
        self.assertEqual(len(monthly.get_json()["trend"]), 12)
        export = self.client.get("/export/analytics.csv?days=7")
        self.assertEqual(export.status_code, 200)
        self.assertIn("pulsedesk-analytics-7d.csv", export.headers["Content-Disposition"])
        self.assertIn("Team member", export.get_data(as_text=True))
        self.assertIn("Bucket", export.get_data(as_text=True))

    def test_employee_cannot_access_analytics_or_analytics_export(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/reports").status_code, 403)
        self.assertEqual(employee.get("/api/analytics").status_code, 403)
        self.assertEqual(employee.get("/export/analytics.csv").status_code, 403)

    def test_csv_export_contains_headers_and_ticket_rows(self):
        response = self.client.get("/export/tickets.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual(rows[0][0], "Ticket ID")
        self.assertGreater(len(rows), 10)

    def test_metrics_endpoint_returns_json_counts(self):
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 12)
        self.assertEqual(data["open"], 9)
        self.assertIn("sla_health", data)
        self.assertIn("Critical", data["by_priority"])

    def test_missing_ticket_returns_custom_404(self):
        response = self.client.get("/tickets/99999")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"went off the grid", response.data)

    def test_knowledge_renderer_escapes_untrusted_html(self):
        with app.app_context():
            rendered = app.jinja_env.filters["article_html"]("<script>alert(1)</script>")
        self.assertNotIn("<script>", str(rendered))
        self.assertIn("&lt;script&gt;", str(rendered))


if __name__ == "__main__":
    unittest.main()
