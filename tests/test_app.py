import csv
import gc
import io
import tempfile
import unittest
from pathlib import Path

from app import app, init_db, recommend_priority


class PulseDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-pulsedesk.db"
        app.config.update(TESTING=True, DATABASE=str(self.db_path))
        init_db()
        self.client = app.test_client()

    def tearDown(self):
        self.client = None
        gc.collect()
        self.temp_dir.cleanup()

    def test_dashboard_loads_seeded_workspace(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Good to see you, Alex", response.data)
        self.assertIn(b"Recent incidents", response.data)

    def test_sla_watchlist_only_returns_unresolved_overdue_tickets(self):
        import re
        import sqlite3
        from datetime import datetime

        response = self.client.get("/tickets?sla=breached")
        self.assertEqual(response.status_code, 200)
        ids = [int(value) for value in re.findall(rb'data-href="/tickets/([0-9]+)"', response.data)]
        self.assertGreater(len(ids), 0)
        self.assertIn(b'name="sla" value="breached"', response.data)
        connection = sqlite3.connect(self.db_path)
        try:
            for ticket_id in ids:
                status, due_at = connection.execute(
                    "SELECT status, due_at FROM tickets WHERE id = ?", (ticket_id,)
                ).fetchone()
                self.assertNotEqual(status, "Resolved")
                self.assertLess(datetime.fromisoformat(due_at), datetime.now())
        finally:
            connection.close()

    def test_priority_recommendation_flags_service_outage(self):
        priority, reason = recommend_priority("Finance server down", "All users cannot access it", "Network")
        self.assertEqual(priority, "Critical")
        self.assertIn("service-wide", reason)

    def test_create_incident_and_smart_triage(self):
        response = self.client.post("/tickets", data={
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
        response = self.client.post("/tickets/1", data={
            "status": "Resolved", "priority": "Medium", "assignee": "Maya Support",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Resolved", response.data)
        self.assertIn(b"Maya Support", response.data)

    def test_invalid_ticket_workflow_values_are_rejected(self):
        response = self.client.post("/tickets/1", data={
            "status": "Delete everything", "priority": "Impossible", "assignee": "Someone",
        })
        self.assertEqual(response.status_code, 400)

    def test_asset_can_be_registered(self):
        response = self.client.post("/assets", data={
            "asset_tag": "AST-999", "name": "Test laptop", "asset_type": "Laptop",
            "assigned_to": "Taylor Reed", "department": "Engineering",
            "status": "In use", "warranty_until": "2028-01-01",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AST-999", response.data)
        self.assertIn(b"Test laptop", response.data)

    def test_duplicate_asset_tag_is_handled(self):
        self.client.post("/assets", data={
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
