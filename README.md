# PulseDesk

**An IT Operations & Service Desk Management Platform** built with Python, Flask, SQLite, HTML, CSS and JavaScript.

PulseDesk is a portfolio project demonstrating how an IT team can manage incidents, people, company assets, service levels and escalation workflows in one responsive workspace. The Northstar Studio demo environment is seeded with fictional records so the app is ready to explore after setup.

![PulseDesk 3.0 command dashboard](docs/pulsedesk-dashboard-preview.png)

![PulseDesk analytics suite](docs/pulsedesk-analytics-preview.png)

[View the new sign-in design](docs/pulsedesk-login-preview.png)

## Product capabilities

- **Role-based access:** secure sign-in with Admin, IT Agent and Employee roles.
- **Account administration:** Admins can create team accounts and assign roles; passwords are stored as Werkzeug password hashes.
- **Incident management:** create, search, filter, assign, prioritise and update requests.
- **Employee self-service:** employees can submit requests and view only requests they created.
- **Escalation workflow:** escalate active incidents through Service Lead, IT Manager and Incident Manager levels, with a timestamped reason and actor history.
- **Rule-based smart triage:** suggest priority and response targets from incident keywords and category.
- **SLA monitoring:** identify overdue incidents and surface an SLA watchlist.
- **Asset inventory:** register devices, track assignment, department, repair state and warranty.
- **Knowledge base:** searchable IT help articles and troubleshooting guides.
- **Analytics & reporting:** explore live incident intake/resolution trends, priority mix, current backlog, SLA compliance and team workload/performance.
- **Reporting windows:** filter trends to 7-day or 30-day daily buckets, 90-day weekly buckets, or a 12-month monthly view.
- **Team performance:** compare assigned volume, active workload, resolved incidents, overdue items and average resolution hours by current assignee.
- **CSV exports:** download the incident register or a period-specific analytics report in Excel-friendly UTF-8 format.
- **REST endpoints:** access operational metrics at `/api/metrics` and windowed reporting data at `/api/analytics?days=30`.
- **CSRF protection:** validate tokens on state-changing form submissions.
- **Notification centre:** personal in-app inbox, unread counts, read state and a compact notification popover.
- **Email alerts:** SMTP delivery for new requests, incident status changes and escalations; delivery outcomes are recorded in an admin-visible outbox.
- **Live updates:** protected polling API refreshes activity, notification counts, SLA metrics and visible incident rows every eight seconds.
- **Distinctive operations UI:** a dark control-room interface with live telemetry, signal stream and responsive layouts.
- **Pulse Assist:** explainable incident analysis with priority recommendations, detected impact signals and category-specific investigation checklists.
- **Knowledge matching:** rank existing runbooks using keyword overlap across article titles, summaries and content, with a small category-match boost.
- **Recurring incident radar:** group tickets from a rolling 90-day window into visible keyword themes and link back to the source incidents.
- **Daily operations brief:** calculate today's created/resolved volume, current active/critical/overdue/unassigned workload, recurring themes and recent activity.
- **Assistant API:** role-protected JSON endpoints for incident analysis, daily summary and recurring patterns.

## Analytics definitions

- **Incidents received** are records whose `created_at` timestamp falls inside the selected window. Trend buckets are daily (7/30 days), weekly (90 days) or monthly (12 months).
- **Resolved in period** uses the dedicated `resolved_at` timestamp. Reopening an incident clears it; resolving it again records a new timestamp. Existing resolved records are backfilled from their previous update timestamp during the additive migration.
- **Active and overdue** counts reflect the current register, not just tickets created inside the selected window. SLA compliance is measured against the active queue; resolved incidents are excluded from its denominator.
- **Team attribution** uses each incident's current assignee because this demo does not yet keep a reassignment history. “Assigned in period” counts tickets created in the selected window by their current assignee.
- **Average resolution hours** is calculated from `created_at` to `resolved_at` for incidents resolved during the window. It is shown only where at least one resolution sample exists.

## Pulse Assist rules and interpretation

Pulse Assist is intentionally a transparent local rules engine, not a trained language model or a third-party AI integration.

- Priority suggestions reuse the existing ticket triage rules and add explicit impact signals for service outages, broad impact, blocked work and possible security indicators.
- Investigation steps vary by category. Security-related phrases produce conservative guidance to avoid unexpected MFA approval, preserve alert details and follow the authorised incident-response process.
- Runbooks are ranked from the existing article table using keyword overlap: title matches carry the highest weight, followed by summary and article-content matches; an exact category match adds a small boost.
- Recurrence groups use a fixed keyword-theme dictionary over tickets created within the last 90 days. At least two tickets are required before a theme is shown. These are investigation leads, not proof of duplicate incidents or a shared root cause.
- The daily operations brief uses the computer's local date/time and current ticket records. It is a snapshot at page load, not a scheduled email report.
- Suggestions never update a ticket's priority or status automatically. An IT responder must validate impact, urgency and the recommended action.

Useful endpoints (Admin and IT Agent roles only):

| Endpoint | Purpose |
|---|---|
| `/assistant` | Smart IT Assistant workspace |
| `POST /api/assistant/analyze` | Analyse a supplied incident JSON object (requires session and CSRF token) |
| `/api/assistant/summary` | Today's operational summary |
| `/api/assistant/recurrences?days=90` | Keyword-theme recurrence scan |

## Demo accounts

On a fresh database, PulseDesk seeds three fictional accounts:

| Role | Email | Demo password |
|---|---|---|
| Admin | `admin@pulsedesk.demo` | `AdminDemo!2026` |
| IT Agent | `agent@pulsedesk.demo` | `AgentDemo!2026` |
| Employee | `employee@pulsedesk.demo` | `EmployeeDemo!2026` |

These credentials are intentionally predictable for a portfolio demonstration. **Do not use them for a real organisation or public deployment.** Change or remove the demo accounts before using real data.

## Role permissions

| Capability | Admin | IT Agent | Employee |
|---|:---:|:---:|:---:|
| View operational dashboard | ✓ | ✓ | — |
| Manage incidents | ✓ | ✓ | Own requests only |
| Escalate incidents | ✓ | ✓ | — |
| Manage company assets | ✓ | ✓ | — |
| View reports and export CSV | ✓ | ✓ | — |
| Search the knowledge base | ✓ | ✓ | ✓ |
| Create user accounts | ✓ | — | — |
| Use Pulse Assist and view daily IT brief | ✓ | ✓ | — |

## Escalation levels

| Level | Escalation target |
|---|---|
| 1 | Service Lead |
| 2 | IT Manager |
| 3 | Incident Manager |

Each escalation records the target level, reason, acting user and timestamp. Resolved incidents cannot be escalated, reasons are required, and the workflow stops at level 3.

## Run locally

Requires Python 3.10 or newer.

```powershell
# 1. Create and activate a virtual environment
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
py -m pip install -r requirements.txt

# 3. Start PulseDesk
py app.py
```

Open **http://127.0.0.1:5000** and sign in with one of the demo accounts above.

### Configure real email delivery (optional)

Without SMTP settings, PulseDesk records clearly labelled email previews in the local outbox and does not send messages. To enable outbound alerts, set the following environment variables before starting the app:

```powershell
$env:PULSEDESK_SMTP_HOST = "smtp.your-provider.example"
$env:PULSEDESK_SMTP_PORT = "587"
$env:PULSEDESK_SMTP_FROM = "pulsedesk@your-domain.example"
$env:PULSEDESK_SMTP_USERNAME = "your-smtp-username"
$env:PULSEDESK_SMTP_PASSWORD = "your-smtp-app-password"
$env:PULSEDESK_SMTP_STARTTLS = "1"
py app.py
```

Use an SMTP app password or provider-issued credential, not your everyday email password. Keep credentials in environment variables or a secret manager; never commit them to Git. The Admin notification centre shows whether delivery is configured and records sent, preview and failed outcomes.

## Run the test suite

```powershell
py -m unittest discover -s tests -v
```

Each test uses an isolated temporary database for its test actions. Importing the app also runs the idempotent schema initialiser against the local demo database so any pending additive migrations are applied.

## Project structure

```
pulsedesk/
├── app.py                  # Flask routes, auth, permissions and data logic
├── instance/               # Local SQLite database and generated secret (ignored)
├── static/
│   ├── app.js              # Modal, navigation and live update behaviour
│   ├── analytics.js        # Interactive reporting charts
│   └── style.css           # Responsive product UI
├── templates/
│   ├── assistant.html      # Pulse Assist, daily brief and pattern radar
│   └── ...                 # Other Jinja screens and reusable layout
├── tests/
│   └── test_app.py         # Authentication and workflow integration tests
├── docs/
│   ├── pulsedesk-dashboard-preview.png
│   ├── pulsedesk-analytics-preview.png
│   └── pulsedesk-login-preview.png
├── .gitignore
├── requirements.txt
└── README.md
```

## Engineering and security notes

- Existing PulseDesk databases receive additive schema migrations for accounts, ticket ownership, escalation history and incident resolution timestamps; existing ticket and asset rows are retained.
- SQL statements use parameter binding for user-supplied values.
- Passwords are hashed with Werkzeug's password hashing helpers; plaintext passwords are never stored.
- Session cookies are HTTP-only and use SameSite=Lax. Set `PULSEDESK_SECURE_COOKIES=1` behind HTTPS to enable the Secure cookie flag. CSRF tokens are checked for POST requests.
- The knowledge article renderer escapes source text before emitting its controlled HTML subset.
- Demo records use fictional names. Do not enter real employee or incident information into this demo.
- Smart triage and Pulse Assist use transparent keyword/category rules, not a trained AI model; all recommendations require human review.
- Knowledge matches and recurring themes are heuristic signals, not semantic root-cause analysis or duplicate detection.
- Email alerts are sent only when SMTP host and sender settings are present; otherwise the outbox records a preview rather than claiming delivery.
- The live dashboard uses authenticated eight-second HTTP polling, not a persistent WebSocket connection.

This is a local portfolio demonstration, not a production-ready identity or service-management system. A real deployment needs HTTPS, secure-cookie configuration, MFA, login rate-limiting and lockout controls, password reset procedures, monitored backups, a hardened production WSGI server, and an organisation-specific privacy and retention policy.

## Next iterations

1. Add assignment notifications, SLA calendars and configurable escalation rules.
2. Add an immutable audit event log and richer user lifecycle controls.
3. Add CI, deployment configuration and browser-based end-to-end tests.

## Licence

MIT — see [LICENSE](LICENSE).
