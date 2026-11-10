import csv
import gc
import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

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
        self.assertIn(b"Good to see you, Alex", response.data)
        self.assertIn(b"Recent incidents", response.data)

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
