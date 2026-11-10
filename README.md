# PulseDesk

**An IT Operations & Service Desk Management Platform** built with Python, Flask, SQLite, HTML, CSS and JavaScript.

PulseDesk is a portfolio project demonstrating how an IT team can manage incidents, people, company assets, service levels and escalation workflows in one responsive workspace. The Northstar Studio demo environment is seeded with fictional records so the app is ready to explore after setup.

![PulseDesk 3.0 command dashboard](docs/pulsedesk-dashboard-preview.png)

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
- **Analytics & reporting:** view workload by status, severity and category, plus resolution rate and SLA health.
- **CSV export:** download an Excel-friendly UTF-8 incident register.
- **REST endpoint:** expose operational metrics as JSON at `/api/metrics`.
- **CSRF protection:** validate tokens on state-changing form submissions.
- **Notification centre:** personal in-app inbox, unread counts, read state and a compact notification popover.
- **Email alerts:** SMTP delivery for new requests, incident status changes and escalations; delivery outcomes are recorded in an admin-visible outbox.
- **Live updates:** protected polling API refreshes activity, notification counts, SLA metrics and visible incident rows every eight seconds.
- **Distinctive operations UI:** a dark control-room interface with live telemetry, signal stream and responsive layouts.

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
│   ├── app.js              # Modal, navigation and interaction behaviour
│   └── style.css           # Responsive product UI
├── templates/              # Jinja screens and reusable layout
├── tests/
│   └── test_app.py         # Authentication and workflow integration tests
├── docs/
│   ├── pulsedesk-dashboard-preview.png
│   └── pulsedesk-login-preview.png
├── .gitignore
├── requirements.txt
└── README.md
```

## Engineering and security notes

- Existing PulseDesk databases receive additive schema migrations for accounts, ticket ownership and escalation history; existing ticket and asset rows are retained.
- SQL statements use parameter binding for user-supplied values.
- Passwords are hashed with Werkzeug's password hashing helpers; plaintext passwords are never stored.
- Session cookies are HTTP-only and use SameSite=Lax. Set `PULSEDESK_SECURE_COOKIES=1` behind HTTPS to enable the Secure cookie flag. CSRF tokens are checked for POST requests.
- The knowledge article renderer escapes source text before emitting its controlled HTML subset.
- Demo records use fictional names. Do not enter real employee or incident information into this demo.
- Smart triage is transparent, rule-based assistance—not a trained AI model or a substitute for human impact assessment.
- Email alerts are sent only when SMTP host and sender settings are present; otherwise the outbox records a preview rather than claiming delivery.
- The live dashboard uses authenticated eight-second HTTP polling, not a persistent WebSocket connection.

This is a local portfolio demonstration, not a production-ready identity or service-management system. A real deployment needs HTTPS, secure-cookie configuration, MFA, login rate-limiting and lockout controls, password reset procedures, monitored backups, a hardened production WSGI server, and an organisation-specific privacy and retention policy.

## Next iterations

1. Add assignment notifications, SLA calendars and configurable escalation rules.
2. Add an immutable audit event log and richer user lifecycle controls.
3. Add CI, deployment configuration and browser-based end-to-end tests.

## Licence

MIT — see [LICENSE](LICENSE).
