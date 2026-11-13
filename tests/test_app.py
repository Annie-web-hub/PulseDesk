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
import worker as worker_module
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

    def test_asset_register_saves_procurement_fields_and_initial_timeline_event(self):
        response = self.post("/assets", {
            "asset_tag": "AST-999", "name": "Test laptop", "asset_type": "Laptop",
            "assigned_to": "Taylor Reed", "department": "Engineering",
            "status": "In use", "warranty_until": "2028-01-01",
            "serial_number": "SN-TEST-999", "vendor": "Example Devices",
            "purchase_date": "2026-10-01", "purchase_cost": "1499.95",
            "location": "Floor 2 · Desk 14", "maintenance_interval_days": "180",
            "next_maintenance_at": "2027-01-01", "notes": "Demo lifecycle record.",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AST-999", response.data)
        self.assertIn(b"Asset timeline", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                """SELECT name, assigned_to, serial_number, vendor, purchase_date,
                purchase_cost, location, warranty_until, next_maintenance_at, notes
                FROM assets WHERE asset_tag='AST-999'"""
            ).fetchone()
            event = db.execute(
                "SELECT action, actor_name FROM asset_events WHERE asset_id=(SELECT id FROM assets WHERE asset_tag='AST-999') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            audit = db.execute(
                "SELECT action FROM audit_events WHERE entity_type='asset' AND entity_id=(SELECT CAST(id AS TEXT) FROM assets WHERE asset_tag='AST-999') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, (
            "Test laptop", "Taylor Reed", "SN-TEST-999", "Example Devices",
            "2026-10-01", 1499.95, "Floor 2 · Desk 14", "2028-01-01",
            "2027-01-01", "Demo lifecycle record.",
        ))
        self.assertEqual(event, ("Asset registered", "Alex Morgan"))
        self.assertEqual(audit[0], "Asset registered")

    def test_asset_profile_changes_are_audited_and_render_in_timeline(self):
        response = self.post("/assets/1", {
            "name": "MacBook Pro 14 - Reassigned",
            "asset_type": "Laptop", "assigned_to": "Taylor Reed",
            "department": "Engineering", "status": "In repair",
            "serial_number": "MBP-001", "vendor": "Apple",
            "purchase_date": "2025-08-12", "purchase_cost": "1899.00",
            "warranty_until": "2027-08-12", "location": "IT Workshop",
            "next_maintenance_at": "2026-12-01",
            "maintenance_interval_days": "120",
            "notes": "Battery diagnostics required.",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset details updated", response.data)
        self.assertIn(b"Taylor Reed", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT assigned_to, department, status, location, purchase_cost FROM assets WHERE id=1"
            ).fetchone()
            event = db.execute(
                "SELECT action, summary FROM asset_events WHERE asset_id=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, ("Taylor Reed", "Engineering", "In repair", "IT Workshop", 1899.0))
        self.assertEqual(event[0], "Asset details updated")
        self.assertIn("assigned to", event[1])
        self.assertIn("status", event[1])

    def test_asset_maintenance_schedule_completion_and_next_due_recalculation(self):
        self.post("/assets", {
            "asset_tag": "AST-MAINT", "name": "Maintenance test device",
            "asset_type": "Laptop", "assigned_to": "Unassigned",
            "department": "Engineering", "status": "In stock",
        })
        with sqlite3.connect(self.db_path) as db:
            asset_id = db.execute("SELECT id FROM assets WHERE asset_tag='AST-MAINT'").fetchone()[0]
        first_due = (datetime.now().date() + timedelta(days=10)).isoformat()
        second_due = (datetime.now().date() + timedelta(days=20)).isoformat()
        self.post(f"/assets/{asset_id}/maintenance", {
            "task": "Annual endpoint health check", "due_at": first_due,
        }, follow_redirects=True)
        self.post(f"/assets/{asset_id}/maintenance", {
            "task": "Battery capacity review", "due_at": second_due,
        }, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            tasks = db.execute(
                "SELECT id, due_at FROM asset_maintenance WHERE asset_id=? AND status='Scheduled' ORDER BY due_at",
                (asset_id,),
            ).fetchall()
            next_due = db.execute("SELECT next_maintenance_at FROM assets WHERE id=?", (asset_id,)).fetchone()[0]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(next_due, first_due)

        maintenance_id = tasks[0][0]
        response = self.post(
            f"/assets/{asset_id}/maintenance/{maintenance_id}/complete",
            {"outcome": "Firmware updated; endpoint checks passed."},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maintenance completed", response.data)
        with sqlite3.connect(self.db_path) as db:
            completed = db.execute(
                "SELECT status, outcome FROM asset_maintenance WHERE id=?",
                (maintenance_id,),
            ).fetchone()
            next_due_after = db.execute(
                "SELECT next_maintenance_at FROM assets WHERE id=?", (asset_id,)
            ).fetchone()[0]
            events = db.execute(
                "SELECT action FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 3",
                (asset_id,),
            ).fetchall()
        self.assertEqual(completed, ("Completed", "Firmware updated; endpoint checks passed."))
        self.assertEqual(next_due_after, second_due)
        self.assertEqual(events[0][0], "Maintenance completed")
        self.assertEqual(events[1][0], "Maintenance scheduled")

        repeated = self.post(
            f"/assets/{asset_id}/maintenance/{maintenance_id}/complete",
            {"outcome": "Duplicate completion attempt."},
            follow_redirects=True,
        )
        self.assertEqual(repeated.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            event_count = db.execute(
                "SELECT COUNT(*) FROM asset_events WHERE asset_id=? AND action='Maintenance completed'",
                (asset_id,),
            ).fetchone()[0]
        self.assertEqual(event_count, 1)

    def test_asset_lifecycle_metrics_and_watch_filters_reflect_attention(self):
        today = datetime.now().date()
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE assets SET warranty_until=''")
            db.execute(
                "UPDATE assets SET warranty_until=? WHERE id=1",
                ((today + timedelta(days=15)).isoformat(),),
            )
            db.execute(
                "UPDATE assets SET warranty_until=? WHERE id=2",
                ((today - timedelta(days=5)).isoformat(),),
            )
            db.execute(
                "UPDATE assets SET next_maintenance_at=? WHERE id=3",
                ((today - timedelta(days=1)).isoformat(),),
            )
            db.commit()
        warranty = self.client.get("/assets?watch=warranty")
        self.assertEqual(warranty.status_code, 200)
        self.assertIn(b"AST-001", warranty.data)
        self.assertIn(b"AST-002", warranty.data)
        warranty_table = warranty.data.split(b'<table class="data-table asset-lifecycle-table">', 1)[1].split(b"</table>", 1)[0]
        self.assertNotIn(b"AST-003", warranty_table)
        maintenance = self.client.get("/assets?watch=maintenance")
        self.assertEqual(maintenance.status_code, 200)
        self.assertIn(b"AST-003", maintenance.data)
        metrics = self.client.get("/api/assets/lifecycle")
        self.assertEqual(metrics.status_code, 200)
        payload = metrics.get_json()
        self.assertEqual(payload["metrics"]["warranty_expiring"], 1)
        self.assertEqual(payload["metrics"]["warranty_expired"], 1)
        self.assertEqual(payload["metrics"]["maintenance_due"], 1)
        self.assertEqual(payload["attention_count"], 3)

    def test_asset_export_contains_lifecycle_fields_and_is_audited(self):
        response = self.client.get("/export/assets.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        contents = response.get_data(as_text=True)
        self.assertIn("Serial number", contents)
        self.assertIn("Purchase cost (EUR)", contents)
        self.assertIn("Maintenance interval (days)", contents)
        self.assertIn("AST-001", contents)
        with sqlite3.connect(self.db_path) as db:
            event = db.execute(
                "SELECT action FROM audit_events WHERE entity_type='report' AND entity_id='asset-inventory' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event[0], "Asset inventory exported")

    def test_asset_lifecycle_access_is_restricted_from_employees(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/assets").status_code, 403)
        self.assertEqual(employee.get("/assets/1").status_code, 403)
        self.assertEqual(employee.get("/api/assets/lifecycle").status_code, 403)
        self.assertEqual(employee.get("/export/assets.csv").status_code, 403)
        with employee.session_transaction() as session:
            csrf = session["_csrf_token"]
        self.assertEqual(employee.post("/assets/1/maintenance", data={
            "task": "Unauthorised", "due_at": "2027-01-01", "csrf_token": csrf,
        }).status_code, 403)

    def test_existing_assets_receive_one_baseline_history_entry_without_duplicate_migration(self):
        with sqlite3.connect(self.db_path) as db:
            asset_count = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            event_count = db.execute(
                "SELECT COUNT(DISTINCT asset_id) FROM asset_events"
            ).fetchone()[0]
            first_event_count = db.execute(
                "SELECT COUNT(*) FROM asset_events WHERE action='Inventory baseline'"
            ).fetchone()[0]
        self.assertEqual(event_count, asset_count)
        self.assertEqual(first_event_count, asset_count)
        init_db()
        with sqlite3.connect(self.db_path) as db:
            repeated_count = db.execute(
                "SELECT COUNT(*) FROM asset_events WHERE action='Inventory baseline'"
            ).fetchone()[0]
        self.assertEqual(repeated_count, asset_count)

    def test_asset_event_timeline_is_append_only(self):
        with app.app_context():
            db = app_module.get_db()
            app_module.write_asset_event(
                db, 1, "Test lifecycle event", "Do not rewrite this history.",
                1, "Alex Morgan", {"source": "unit-test"},
            )
            db.commit()
        with sqlite3.connect(self.db_path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE asset_events SET summary='rewritten' WHERE summary='Do not rewrite this history.'"
                )
            db.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "DELETE FROM asset_events WHERE summary='Do not rewrite this history.'"
                )
            db.rollback()
            row = db.execute(
                "SELECT action, actor_name, metadata_json FROM asset_events WHERE summary='Do not rewrite this history.'"
            ).fetchone()
        self.assertEqual(row[0:2], ("Test lifecycle event", "Alex Morgan"))
        self.assertIn("unit-test", row[2])

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

    def test_pulse_assistant_recommends_priority_with_explainable_signals(self):
        with app.app_context():
            db = app_module.get_db()
            result = app_module.analyse_incident(
                db,
                "VPN outage affecting all users",
                "The whole team is blocked and internal tools are unavailable.",
                "Network",
            )
        self.assertEqual(result["suggested_priority"], "Critical")
        self.assertEqual(result["response_target"], "Immediately")
        self.assertIn("Broad impact", {item["label"] for item in result["signals"]})
        self.assertIn("Service outage", {item["label"] for item in result["signals"]})
        self.assertIn("Confirm whether the issue affects one device", result["checks"][0])
        self.assertTrue(result["articles"])
        self.assertIn("VPN", result["articles"][0]["title"])
        self.assertIn("human", result["review_note"].lower())

    def test_pulse_assistant_handles_security_signals_safely(self):
        with app.app_context():
            result = app_module.analyse_incident(
                app_module.get_db(),
                "Suspicious sign-in alert",
                "A user received an unexpected MFA prompt and does not recognise the location.",
                "Access",
            )
        self.assertEqual(result["suggested_priority"], "High")
        self.assertIn("Security signal", {item["label"] for item in result["signals"]})
        self.assertTrue(any("Do not approve" in item for item in result["checks"]))
        self.assertTrue(any("suspicious sign-in" in item["title"].lower() for item in result["articles"]))

    def test_knowledge_matcher_ranks_relevant_existing_runbooks(self):
        with app.app_context():
            matches = app_module.match_knowledge_articles(
                app_module.get_db(), "company VPN disconnects from home network", "Network"
            )
        self.assertTrue(matches)
        self.assertEqual(matches[0]["title"], "Connect to the company VPN")
        self.assertIn("vpn", matches[0]["matched_terms"])

    def test_recurring_incident_detector_groups_real_recent_ticket_themes(self):
        now = datetime.now().replace(microsecond=0)
        with sqlite3.connect(self.db_path) as db:
            rows = []
            for index, title in enumerate((
                "VPN connection unstable for finance team",
                "Network VPN disconnects after a few minutes",
            )):
                created = (now - timedelta(days=index + 1)).isoformat(timespec="seconds")
                rows.append((
                    title, "Users lose VPN connectivity and cannot reach internal tools.",
                    "Sample User", "Finance", "Network", "High", "Open", "Jordan Lee",
                    created, created, (now + timedelta(days=1)).isoformat(timespec="seconds"),
                ))
            db.executemany(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            db.commit()
        with app.app_context():
            tickets = app_module.get_db().execute("SELECT * FROM tickets").fetchall()
            patterns = app_module.detect_recurring_incidents(tickets, 90)
        vpn = next(item for item in patterns if item["theme"] == "VPN & connectivity")
        self.assertGreaterEqual(vpn["count"], 3)
        self.assertGreaterEqual(vpn["open_count"], 2)
        self.assertTrue(vpn["ticket_ids"])

    def test_daily_operations_summary_uses_current_database_records(self):
        now = datetime.now().replace(microsecond=0)
        created = now.isoformat(timespec="seconds")
        due = (now + timedelta(hours=2)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Today's assistant test ticket", "A current test incident.", "Test Requester",
                 "Engineering", "Software", "Critical", "Open", "Unassigned",
                 created, created, due),
            )
            db.commit()
        with app.app_context():
            summary = app_module.build_daily_operations_summary(app_module.get_db())
        self.assertEqual(summary["created_today"], 1)
        self.assertEqual(summary["active"], 10)
        self.assertEqual(summary["critical"], 3)
        self.assertGreaterEqual(summary["unassigned"], 1)
        self.assertTrue(any("critical incident" in item.lower() for item in summary["highlights"]))

    def test_pulse_assistant_page_renders_analysis_and_daily_summary(self):
        response = self.client.get("/assistant")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Turn the noise", response.data)
        self.assertIn(b"Today's pulse", response.data)
        self.assertIn(b"Recurring incident themes", response.data)
        response = self.post("/assistant", {
            "title": "VPN outage affecting every department",
            "description": "All users are unable to connect.",
            "category": "Network",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CRITICAL SUGGESTED", response.data)
        self.assertIn(b"Runbooks worth opening", response.data)

    def test_assistant_api_requires_role_and_csrf_and_returns_analysis(self):
        with self.client.session_transaction() as session:
            csrf = session["_csrf_token"]
        response = self.client.post(
            "/api/assistant/analyze",
            json={
                "title": "Suspicious sign-in across multiple departments",
                "description": "Multiple users report suspicious sign-in alerts.",
                "category": "Access",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn(payload["suggested_priority"], ("High", "Critical"))
        self.assertIn("signals", payload)
        missing = self.client.post(
            "/api/assistant/analyze",
            json={"description": "No title supplied."},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(missing.status_code, 400)
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/assistant").status_code, 403)
        self.assertEqual(employee.get("/api/assistant/summary").status_code, 403)
        self.assertEqual(employee.get("/api/assistant/recurrences").status_code, 403)

    def test_assistant_summary_and_recurring_pattern_apis_return_json(self):
        summary = self.client.get("/api/assistant/summary")
        self.assertEqual(summary.status_code, 200)
        self.assertIn("created_today", summary.get_json())
        recurring = self.client.get("/api/assistant/recurrences?days=400")
        self.assertEqual(recurring.status_code, 200)
        self.assertEqual(recurring.get_json()["days"], 365)
        self.assertIn("patterns", recurring.get_json())

    def test_business_minute_clock_excludes_nights_and_weekends(self):
        calendar = {
            "timezone_name": "Europe/Riga",
            "business_start": "09:00",
            "business_end": "17:00",
            "weekdays": "0,1,2,3,4",
        }
        friday = datetime(2026, 11, 13, 16, 0)
        monday = datetime(2026, 11, 16, 10, 0)
        self.assertEqual(app_module.business_minutes_between(friday, monday, calendar), 120)
        outside_hours = datetime(2026, 11, 13, 17, 0)
        weekend = datetime(2026, 11, 15, 12, 0)
        self.assertEqual(app_module.business_minutes_between(outside_hours, weekend, calendar), 0)
        same_day = app_module.business_minutes_between(
            datetime(2026, 11, 16, 8, 0), datetime(2026, 11, 16, 18, 0), calendar
        )
        self.assertEqual(same_day, 480)

    def test_monitoring_page_shows_worker_calendar_and_mail_status(self):
        response = self.client.get("/monitoring")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monitoring & integrations", response.data)
        self.assertIn(b"Europe/Riga", response.data)
        self.assertIn(b"py worker.py", response.data)
        self.assertIn(b"Enable scheduled SLA checks", response.data)
        self.assertIn(b"Preview mode", response.data)
        health = self.client.get("/api/monitoring/health")
        self.assertEqual(health.status_code, 200)
        payload = health.get_json()
        self.assertTrue(payload["database"]["ok"])
        self.assertFalse(payload["scheduler"]["enabled"])
        self.assertFalse(payload["worker"]["online"])
        self.assertFalse(payload["smtp"]["configured"])

    def test_admin_can_save_business_calendar_and_scheduler_setting(self):
        response = self.post("/monitoring/calendar", {
            "timezone_name": "Europe/Riga",
            "business_start": "08:30",
            "business_end": "16:30",
            "weekdays": ["0", "1", "2", "3", "4", "5"],
            "interval_seconds": "120",
            "scheduler_enabled": "1",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monitoring settings saved", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT timezone_name, business_start, business_end, weekdays, scheduler_enabled, interval_seconds FROM sla_calendar WHERE id=1"
            ).fetchone()
            audit = db.execute(
                "SELECT action, summary FROM audit_events WHERE entity_type='sla_calendar' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, ("Europe/Riga", "08:30", "16:30", "0,1,2,3,4,5", 1, 120))
        self.assertEqual(audit[0], "Monitoring settings updated")
        self.assertIn("automatic checks enabled", audit[1])
        health = self.client.get("/api/monitoring/health").get_json()
        self.assertTrue(health["scheduler"]["enabled"])
        self.assertEqual(health["scheduler"]["interval_seconds"], 120)

    def test_invalid_monitoring_calendar_is_rejected_without_saving(self):
        response = self.post("/monitoring/calendar", {
            "timezone_name": "Mars/Olympus",
            "business_start": "18:00",
            "business_end": "09:00",
            "weekdays": ["0"],
            "interval_seconds": "15",
            "scheduler_enabled": "1",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter a valid timezone", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT timezone_name, scheduler_enabled FROM sla_calendar WHERE id=1"
            ).fetchone()
        self.assertEqual(row, ("Europe/Riga", 0))

    def test_monitoring_and_calendar_configuration_respect_roles(self):
        agent = self.login_client("agent@pulsedesk.demo", "AgentDemo!2026")
        self.assertEqual(agent.get("/monitoring").status_code, 200)
        with agent.session_transaction() as session:
            csrf = session["_csrf_token"]
        self.assertEqual(agent.post("/monitoring/calendar", data={
            "timezone_name": "Europe/Riga", "business_start": "09:00",
            "business_end": "17:00", "weekdays": ["0", "1", "2", "3", "4"],
            "interval_seconds": "60", "csrf_token": csrf,
        }).status_code, 403)
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/monitoring").status_code, 403)
        self.assertEqual(employee.get("/api/monitoring/health").status_code, 403)

    def test_admin_smtp_test_creates_preview_when_smtp_is_missing(self):
        with patch.object(app_module, "smtp_is_configured", return_value=False):
            response = self.post("/monitoring/test-email", {}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no email was sent", response.data)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT recipient, subject, status FROM email_outbox ORDER BY id DESC LIMIT 1"
            ).fetchone()
            audit = db.execute(
                "SELECT action FROM audit_events WHERE entity_type='integration' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, ("admin@pulsedesk.demo", "PulseDesk SMTP connectivity test", "preview"))
        self.assertEqual(audit[0], "SMTP test requested")

    def test_worker_is_paused_by_default_and_rejects_a_second_instance(self):
        claimed, result = worker_module.run_cycle("worker-test-one")
        self.assertTrue(claimed)
        self.assertEqual(result["status"], "paused")
        duplicate, busy = worker_module.run_cycle("worker-test-two")
        self.assertFalse(duplicate)
        self.assertEqual(busy["status"], "busy")
        with sqlite3.connect(self.db_path) as db:
            state = db.execute(
                "SELECT status, owner_token FROM worker_state WHERE id=1"
            ).fetchone()
        self.assertEqual(state, ("paused", "worker-test-one"))
        worker_module.release_worker("worker-test-one")
        with sqlite3.connect(self.db_path) as db:
            state = db.execute(
                "SELECT status, owner_token FROM worker_state WHERE id=1"
            ).fetchone()
        self.assertEqual(state, ("offline", None))

    def test_worker_runs_scheduled_escalation_only_after_opt_in(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE tickets SET status='Resolved'")
            db.execute("UPDATE workflow_rules SET is_active=0")
            db.execute(
                "UPDATE workflow_rules SET is_active=1 WHERE priority='High' AND escalation_level=1"
            )
            db.execute("UPDATE sla_calendar SET scheduler_enabled=1, interval_seconds=30 WHERE id=1")
            db.execute(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Worker scheduled test", "Used to verify opt-in business-hours escalation.",
                 "Test User", "Engineering", "Network", "High", "Open", "Unassigned",
                 "2026-11-13T15:00:00", "2026-11-13T15:00:00", "2026-11-13T16:00:00"),
            )
            db.commit()
            ticket_id = db.execute(
                "SELECT id FROM tickets WHERE title='Worker scheduled test'"
            ).fetchone()[0]
        with patch.object(app_module, "calendar_now", return_value=datetime(2026, 11, 16, 10, 0)):
            claimed, result = worker_module.run_cycle("worker-opt-in-test")
        self.assertTrue(claimed)
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["checked"])
        self.assertEqual(result["result"]["escalated_count"], 1)
        with sqlite3.connect(self.db_path) as db:
            level = db.execute(
                "SELECT escalation_level FROM tickets WHERE id=?", (ticket_id,)
            ).fetchone()[0]
            event = db.execute(
                "SELECT actor_name, action FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
                (str(ticket_id),),
            ).fetchone()
        self.assertEqual(level, 1)
        self.assertEqual(event, ("PulseDesk SLA Worker", "SLA escalation"))
        worker_module.release_worker("worker-opt-in-test")

    def test_workflow_engine_page_renders_default_policy_matrix(self):
        response = self.client.get("/workflow")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Workflow engine", response.data)
        self.assertIn(b"Run escalation check", response.data)
        self.assertIn(b"Critical", response.data)
        self.assertIn(b"4320", response.data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM workflow_rules").fetchone()[0], 12)

    def test_admin_can_edit_sla_rule_and_change_is_audited(self):
        page = self.client.get("/workflow")
        self.assertIn(b'name="priority" value="Critical"', page.data)
        self.assertIn(b'name="escalation_level" value="1"', page.data)
        response = self.post("/workflow/rules/save", {
            "rule_id": "1", "priority": "Critical", "escalation_level": "1",
            "overdue_minutes": "12", "is_active": "1",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            rule = db.execute(
                "SELECT overdue_minutes, target_role, is_active FROM workflow_rules WHERE id=1"
            ).fetchone()
            event = db.execute(
                "SELECT action, summary FROM audit_events WHERE entity_type='workflow_rule' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(rule, (12, "Service Lead", 1))
        self.assertEqual(event[0], "Rule saved")
        self.assertIn("12 minutes", event[1])

    def test_it_agent_can_run_workflow_but_cannot_change_policy(self):
        agent = self.login_client("agent@pulsedesk.demo", "AgentDemo!2026")
        self.assertEqual(agent.get("/workflow").status_code, 200)
        self.assertEqual(agent.get("/audit-log").status_code, 200)
        with agent.session_transaction() as session:
            csrf = session["_csrf_token"]
        response = agent.post("/workflow/rules/save", data={
            "rule_id": "1", "priority": "Critical", "escalation_level": "1",
            "overdue_minutes": "99", "is_active": "1", "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_edit_or_run_workflow_policies(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/workflow").status_code, 403)
        self.assertEqual(employee.get("/audit-log").status_code, 403)
        self.assertEqual(employee.get("/export/daily-operations.csv").status_code, 403)
        self.assertEqual(employee.post("/workflow/run-sla", data={
            "csrf_token": self.csrf_for(employee, "/knowledge")
        }).status_code, 403)
        self.assertEqual(employee.post("/workflow/rules/save", data={
            "priority": "Critical", "escalation_level": 1,
            "overdue_minutes": 0, "csrf_token": self.csrf_for(employee, "/knowledge")
        }).status_code, 403)

    def test_sla_workflow_escalates_one_configured_stage_per_manual_run(self):
        now = datetime.now().replace(microsecond=0)
        created = (now - timedelta(hours=10)).isoformat(timespec="seconds")
        due = (now - timedelta(hours=8)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Workflow critical test", "A critical service outage used for testing.", "Test User",
                 "Engineering", "Network", "Critical", "Open", "Unassigned",
                 created, created, due),
            )
            ticket_id = cursor.lastrowid
            db.commit()

        first = self.post("/workflow/run-sla", {}, follow_redirects=True)
        self.assertEqual(first.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            level = db.execute("SELECT escalation_level FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
            rows = db.execute("SELECT level, target_role FROM ticket_escalations WHERE ticket_id=? ORDER BY level", (ticket_id,)).fetchall()
        self.assertEqual(level, 1)
        self.assertEqual(rows, [(1, "Service Lead")])

        self.post("/workflow/run-sla", {}, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            level = db.execute("SELECT escalation_level FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
            rows = db.execute("SELECT level FROM ticket_escalations WHERE ticket_id=? ORDER BY level", (ticket_id,)).fetchall()
        self.assertEqual(level, 2)
        self.assertEqual(rows, [(1,), (2,)])

        self.post("/workflow/run-sla", {}, follow_redirects=True)
        with sqlite3.connect(self.db_path) as db:
            level = db.execute("SELECT escalation_level FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
            rows = db.execute("SELECT level FROM ticket_escalations WHERE ticket_id=? ORDER BY level", (ticket_id,)).fetchall()
            audit = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=? AND action='SLA escalation'",
                (str(ticket_id),),
            ).fetchone()[0]
        self.assertEqual(level, 3)
        self.assertEqual(rows, [(1,), (2,), (3,)])
        self.assertEqual(audit, 3)

    def test_sla_workflow_does_not_escalate_before_the_due_time(self):
        now = datetime.now().replace(microsecond=0)
        created = now.isoformat(timespec="seconds")
        due = (now + timedelta(hours=1)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute(
                """INSERT INTO tickets
                (title, description, requester, department, category, priority, status, assignee,
                 created_at, updated_at, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Not yet overdue", "An upcoming critical response deadline.", "Test User",
                 "Engineering", "Network", "Critical", "Open", "Unassigned",
                 created, created, due),
            )
            ticket_id = cursor.lastrowid
            db.commit()
        result = self.post("/workflow/run-sla", {}, follow_redirects=True)
        self.assertEqual(result.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            ticket = db.execute(
                "SELECT escalation_level FROM tickets WHERE id=?", (ticket_id,)
            ).fetchone()[0]
            count = db.execute(
                "SELECT COUNT(*) FROM ticket_escalations WHERE ticket_id=?", (ticket_id,)
            ).fetchone()[0]
        self.assertEqual(ticket, 0)
        self.assertEqual(count, 0)

    def test_assignment_suggestions_rank_real_active_it_accounts(self):
        response = self.client.get("/api/tickets/3/assignment-suggestions")
        self.assertEqual(response.status_code, 200)
        suggestions = response.get_json()["suggestions"]
        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["name"], "Alex Morgan")
        self.assertIn("Lowest weighted workload", suggestions[0]["reason"])
        self.assertNotIn("Maya Patel", {item["name"] for item in suggestions})

    def test_assignment_requires_explicit_valid_confirmation_and_records_audit(self):
        suggestions = self.client.get("/api/tickets/3/assignment-suggestions")
        self.assertEqual(suggestions.status_code, 200)
        response = self.post("/tickets/3/assign", {"assignee": "Alex Morgan"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            assignee = db.execute("SELECT assignee FROM tickets WHERE id=3").fetchone()[0]
            event = db.execute(
                "SELECT action, summary FROM audit_events WHERE entity_id='3' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(assignee, "Alex Morgan")
        self.assertEqual(event[0], "Assignee changed")
        self.assertIn("Alex Morgan", event[1])

        invalid = self.post("/tickets/3/assign", {"assignee": "Not an IT account"}, follow_redirects=True)
        self.assertEqual(invalid.status_code, 200)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT assignee FROM tickets WHERE id=3").fetchone()[0], "Alex Morgan")

    def test_employee_cannot_assign_incidents_or_read_assignment_suggestions(self):
        employee = self.login_client("employee@pulsedesk.demo", "EmployeeDemo!2026")
        self.assertEqual(employee.get("/api/tickets/1/assignment-suggestions").status_code, 403)
        with employee.session_transaction() as session:
            csrf = session["_csrf_token"]
        response = employee.post("/tickets/1/assign", data={
            "assignee": "Alex Morgan", "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 403)

    def test_audit_events_are_append_only_in_the_database(self):
        with app.app_context():
            db = app_module.get_db()
            app_module.write_audit_event(
                db, "test", "immutable-1", "Test event", "Cannot rewrite this event.",
                1, "Alex Morgan", {"origin": "unit-test"},
            )
            db.commit()
        with sqlite3.connect(self.db_path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE audit_events SET summary='rewritten' WHERE entity_id='immutable-1'")
            db.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM audit_events WHERE entity_id='immutable-1'")
            db.rollback()
            row = db.execute(
                "SELECT summary, metadata_json FROM audit_events WHERE entity_id='immutable-1'"
            ).fetchone()
        self.assertEqual(row[0], "Cannot rewrite this event.")
        self.assertIn("unit-test", row[1])

    def test_audit_log_search_and_daily_report_export(self):
        self.post("/tickets/1", {
            "status": "Waiting", "priority": "High", "assignee": "Jordan Lee",
        })
        audit = self.client.get("/audit-log?q=Ticket")
        self.assertEqual(audit.status_code, 200)
        self.assertIn(b"Ticket updated", audit.data)
        report = self.client.get("/export/daily-operations.csv")
        self.assertEqual(report.status_code, 200)
        self.assertIn("text/csv", report.content_type)
        self.assertIn("pulsedesk-daily-operations-", report.headers["Content-Disposition"])
        contents = report.get_data(as_text=True)
        self.assertIn("PulseDesk daily operations brief", contents)
        self.assertIn("Active queue", contents)
        self.assertIn("Open incident register", contents)
        with sqlite3.connect(self.db_path) as db:
            self.assertGreaterEqual(
                db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 2
            )

    def test_ticket_detail_shows_workload_assignment_and_audit_context(self):
        response = self.client.get("/tickets/3")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Who has capacity?", response.data)
        self.assertIn(b"Assign selected teammate", response.data)

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
