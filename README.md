# Academic Ace platform

Standalone public site and a small Python/SQLite application for consultation inquiries, owner-managed ZMW estimates, quotations, project tracking, manually recorded payment status, and approved sample resources. Runtime code uses only Python 3.10+ standard-library modules.

## Current implementation and launch limits

- Public inquiry records are saved in SQLite. A successful form response includes the saved inquiry reference.
- Inquiry, quotation, project, milestone, payment-record, sample, pricing, public-contact, session and audit data use relational SQLite tables with foreign-key constraints and indexes.
- Prices are configurable whole-ZMW per-page rates. The seeded development values are examples only and are **not approved commercial prices**. The public calculator labels this state. The owner can publish a new approved configuration in the administrator dashboard.
- Quotations snapshot their price configuration version and amount. The owner shares each private token link manually through a business-approved contact method. A client can accept or decline; accepting creates a project record.
- Payment records are manual administrator records only. They do not initiate, verify, or settle a transaction. No payment provider is selected or connected.
- The sample library starts empty. Admins add permission-cleared text examples and explicitly publish them.
- The owner can configure public email, WhatsApp, Signal and contact-hours details. No business contact values are prefilled or fabricated.
- There is no file upload, email notification, WhatsApp API, client login, automatic inquiry confirmation email, or external payment integration.
- The application does not encrypt the SQLite database file itself. Put the database and backups on encrypted storage, restrict host access, and use HTTPS at the public reverse proxy.
- The published privacy and service pages are implementation-aware drafts. The owner must supply business identity/contact details, approve a retention period, and obtain local legal/privacy review before accepting real client information at scale.

This is a low-volume, single-instance baseline. In production, the included `ThreadingHTTPServer` must sit behind a maintained HTTPS reverse proxy or a hosting platform's managed HTTPS ingress. Use one application instance with a persistent writable database volume; SQLite is not configured for multi-host writes.

## Local setup

Use Python 3.10 or newer. No package installation is required.

### PowerShell

Confirm that `python --version` reports Python 3.10 or newer before running these commands. If Windows resolves `python` to the Microsoft Store alias, install Python from an approved source or use the full path to your managed Python executable in place of `python`.

```powershell
$env:APP_ENV = 'development'
$env:APP_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(48))")
$env:APP_ORIGIN = 'http://127.0.0.1:8000'
$env:HOST = '127.0.0.1'
$env:PORT = '8000'
$env:DATABASE_PATH = '.\data\academic_ace.sqlite3'
$env:RETENTION_DAYS = '365'
$env:TRUST_PROXY = '0'
python server.py migrate
python server.py create-admin
python server.py serve
```

Open `http://127.0.0.1:8000/`. Administrator console: `http://127.0.0.1:8000/admin`.

### macOS / Linux

```sh
export APP_ENV=development
export APP_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export APP_ORIGIN=http://127.0.0.1:8000
export HOST=127.0.0.1
export PORT=8000
export DATABASE_PATH=./data/academic_ace.sqlite3
export RETENTION_DAYS=365
export TRUST_PROXY=0
python3 server.py migrate
python3 server.py create-admin
python3 server.py serve
```

The first administrator created becomes the owner. Later `create-admin` runs prompt before adding administrator accounts. Passwords are hashed with PBKDF2-HMAC-SHA256 (600,000 iterations); sessions use random bearer tokens stored only as keyed hashes and `HttpOnly; SameSite=Strict` cookies. Production cookies also use `Secure`.

Keep `APP_SECRET` stable and secret. Changing it invalidates existing sessions and changes keyed rate-limit hashes. Do not commit `.env` files, production database files, or backups.

## Pricing

Example defaults are in `server.py` and seeded as configuration version 1 with `approved=false`:

| Project type | Example rate per page |
|---|---:|
| Assignment tutoring | K150 |
| Proposal consultation | K220 |
| Thesis coaching | K280 |
| Editing | K90 |

Multipliers and maximum estimator pages are also configurable. These values are for local development only. The business owner must replace and approve real ZMW rates in **Administrator → Pricing configuration**. Each save creates an immutable configuration version; new quotations store a snapshot of the version used. Earlier quote amounts and their snapshot remain unchanged.

The calculator rounds the total to whole kwacha. A 40/30/30 schedule is displayed for a full thesis or more than 20 pages. The last instalment is the remainder after rounding, so installments always add up exactly to the estimate.

## Database, backup and retention

`python server.py migrate` applies numbered SQL files in `migrations/` and seeds the development pricing configuration. The database path is selected with `DATABASE_PATH` and defaults to `data/academic_ace.sqlite3`.

Create a consistent SQLite backup with:

```sh
python server.py backup
```

An optional `--destination /secure/path/academic-ace.sqlite3` selects the backup location. Keep backups encrypted, access-controlled, and governed by the same retention policy as the live database. To recover, stop the server, preserve the current database for investigation, restore the approved backup file to `DATABASE_PATH`, then restart and check `/api/health`.

Set `RETENTION_DAYS` to a business-approved value from 30 to 3,650. Review candidates first:

```sh
python server.py purge-expired --dry-run
```

Run `python server.py purge-expired` only after approving the retention schedule. It removes old declined/closed/quoted/accepted inquiries with no project, and completed/cancelled projects (including related inquiry, quotation, and payment rows) past the retention window. It keeps non-PII audit events. The command is not automatically scheduled; configure an owner-reviewed scheduled job and align backup expiry before production. Do not run a purge in production until the retention policy and legal holds are settled.

## Render free preview

The `render.yaml` Blueprint publishes only `public-preview/` as a free static site. It is a public, informational preview: inquiry submission, pricing, sample records, administrator access, quotations, projects, and payment records are not enabled. The preview explicitly tells visitors not to submit personal or academic information. It does not include the SQLite database, server source, test data, backups, or secret configuration in the published directory.

Render static sites are served from the global CDN and do not run this Python application. The current SQLite-backed business workflow still requires a paid service and persistent database storage before it can safely accept real inquiries. Do not point a free ephemeral web service at the SQLite application: its local database can be lost after restart, spin-down, or redeploy. For other hosts, use the production configuration below.

## Production deployment boundary

1. Provision one host and persistent encrypted storage for `DATABASE_PATH` and backups.
2. Set `APP_ENV=production`, a stable random `APP_SECRET`, `APP_ORIGIN=https://<approved-domain>`, `HOST=127.0.0.1`, a port, database path, and owner-approved `RETENTION_DAYS` in the host's secret/configuration manager. For a managed ingress that requires binding to `0.0.0.0`, set `TRUSTED_HTTPS_INGRESS=1` only when TLS is terminated by that ingress and the application port is not otherwise publicly exposed. Keep `TRUST_PROXY=0` unless the service is behind a trusted proxy that overwrites `X-Forwarded-For`; only then set `TRUST_PROXY=1` so inquiry/login throttling can distinguish visitors.
3. Run migrations, create the owner account interactively over a secure administrative channel, and start the service under a supervised process manager.
4. Put a maintained reverse proxy such as the hosting platform's managed HTTPS ingress in front of the loopback listener. Enforce TLS, request/body limits, security updates, access logging that excludes query strings and sensitive headers, and a tested backup/restore process.
5. Verify the domain, origin configuration, secure cookie, database permissions, backups, monitoring, and retention schedule before taking real inquiries.

No deployment has been performed. No email provider or payment provider has been selected. Before adding one, the owner must choose a supported merchant/notification service, complete its onboarding, configure server-side credentials, and review the provider's current fees, terms, privacy and webhook documentation. Never put provider secrets in frontend code.

## Administrator and workflow

- Create the first owner with `python server.py create-admin`; there is no default credential.
- Reset a forgotten password from a secure local console with `python server.py reset-admin-password owner@example.com`; this invalidates that user's sessions.
- Administrators can review inquiries, change their workflow status and notes, draft quotations, manage approved samples, and update project status.
- Only the owner role can change customer-facing pricing or record payment statuses. All roles and privileged operations are checked on the server.
- Quotation creation produces a private acceptance link but does not send it. Copy the link and share it using the client's selected channel; only then mark it as shared. The acceptance page records the client's response and creates a project record after acceptance.
- Payment entry is a manually recorded status. An administrator must verify payment outside this application; the dashboard does not contact a bank, wallet, or payment provider.

## Tests

Run the integration suite using Python:

```sh
python -m unittest discover -s tests -v
```

It uses a temporary database and ephemeral local HTTP port; it does not access production records, send notifications, or process a payment. The app does not yet include visual browser automation or a production deployment pipeline.

## Business-owner decisions still required

- Legal business name, monitored email/phone/WhatsApp/Signal contact channels, and public contact hours.
- Approved ZMW rate card, currency/tax wording, quote expiry practice, milestone policy, cancellation/refund terms, and service agreement.
- Local legal/privacy review, data location/provider list, retention period, privacy contact and deletion request workflow.
- Production host, domain, TLS/reverse proxy, encrypted persistent storage, process supervision, monitoring, backups and recovery drill.
- Whether to select an email notification provider and/or payment provider; no provider is currently integrated.
- Whether client accounts, uploaded documents, or multi-role staff assignments are actually needed. They are intentionally not represented as working features.
