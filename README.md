# Academic Ace — production release

Academic Ace is a premium Flask-based academic support platform with a responsive marketing site, project intake, server-side estimation, private file handling, client portal, milestone workflow, project messaging and an administrator operations dashboard.

## What v3 adds

- Private client portal access using a one-time displayed access token
- Server-side token hashing; raw portal tokens are not stored
- Project milestone records with percentages, amounts and delivery/payment states
- Admin quote issuance and milestone management
- Client/admin project messaging
- Payment records ready for a real payment provider integration
- CSRF protection for state-changing forms
- Secure session cookie defaults
- PostgreSQL support
- Gunicorn deployment configuration
- Render deployment blueprint
- Private upload directory outside public static assets

## Local setup

Python 3.11+ is recommended.

```bash
python -m venv .venv
# Windows PowerShell
.\\.venv\\Scripts\\Activate.ps1
# macOS/Linux
# source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows CMD
# cp .env.example .env # macOS/Linux
python run.py
```

Open `http://127.0.0.1:5000`.

Set these values in `.env` before using the application:

```text
SECRET_KEY=<long random value>
ADMIN_USERNAME=<your admin username>
ADMIN_PASSWORD=<strong admin password>
DATABASE_URL=sqlite:///instance/academic_ace_v3.db
CURRENCY=ZMW
SESSION_COOKIE_SECURE=0
```

For production, use PostgreSQL and set `SESSION_COOKIE_SECURE=1` behind HTTPS.

## Project workflow

```text
SUBMITTED
  ↓
UNDER REVIEW
  ↓
QUOTE ISSUED
  ↓
AWAITING CONFIRMATION
  ↓
ACTIVE
  ↓
MILESTONE 1
  ↓
MILESTONE 2
  ↓
FINAL QA
  ↓
READY FOR DELIVERY
  ↓
COMPLETED
```

For a thesis/dissertation or a project over roughly 20 pages, Academic Ace automatically creates a 40/30/30 milestone plan. Smaller projects use a single delivery milestone.

## Payment architecture

The current build does **not pretend that a payment has occurred**. Admins can mark milestones as paid manually and the database records payment state. This is intentionally provider-neutral. A real gateway should be connected only after selecting the payment provider(s) appropriate to the business and jurisdiction.

When adding a provider, implement:

1. Server-created checkout/payment intent.
2. Provider webhook signature verification.
3. Idempotent transaction handling.
4. Server-side amount validation.
5. Automatic milestone/payment state updates.
6. No reliance on browser-supplied payment status.

## Production security checklist

Before accepting real student documents:

1. Use a long random `SECRET_KEY`.
2. Use a strong admin password and preferably replace environment-password authentication with an admin user table using Argon2id/bcrypt.
3. Use PostgreSQL.
4. Use private object storage for uploads rather than local disk in a multi-instance deployment.
5. Add malware scanning for PDF/DOCX/XLSX uploads.
6. Validate file signatures/magic bytes in addition to extension and MIME type.
7. Put the app behind HTTPS/TLS.
8. Keep `SESSION_COOKIE_SECURE=1` in production.
9. Add rate limiting to login, portal access and public intake endpoints.
10. Add audit logs for administrator actions.
11. Configure database, file and application backups.
12. Define document retention/deletion policies.
13. Configure transactional email for intake receipts, quote notices and status updates.
14. Review privacy, terms and academic-integrity policies before launch.
15. Do not publish student documents or personal information without appropriate permission.

## Deployment

The repository includes `render.yaml` and `Procfile` for a Render-style deployment. Set production secrets in the hosting dashboard. The application should be served by Gunicorn, use PostgreSQL, and use private object storage for uploaded documents.

## Important

The calculator produces an **indicative estimate**. The authoritative quote is set server-side by an administrator after reviewing project complexity. Browser values must never be trusted for payment amounts.

## v4 operational additions

- Flutterwave hosted checkout adapter
- Server-side transaction verification
- Verified webhook endpoint with secret hash
- Payment records linked to milestones
- Client payment email capture
- `/health` endpoint
- Production security headers
- Deployment and migration notes in `DEPLOYMENT.md`


## Final release notes

- Added Privacy and Terms template pages.
- Added portal-owned private file downloads.
- Added basic login/portal/intake rate limiting.
- Added Office Open XML structure validation for DOCX/XLSX uploads.
- Added configurable maximum file count per project.
- Added password-hash compatibility for administrator authentication.
- Added project-receipt and quote email hooks.

### Before launch

Do not accept real student documents until private persistent storage, malware scanning, PostgreSQL backups, administrator MFA, monitoring, final privacy/terms review and a tested recovery procedure are configured. The included local upload folder is suitable for development; production deployments should use private persistent object storage or an encrypted persistent volume.

### Administrator password hardening

A plaintext environment password is supported for initial setup, but production should use a password hash. Generate one with:

```bash
python -c "from app.security import password_hash; print(password_hash('REPLACE_WITH_A_STRONG_PASSWORD'))"
```

Put the resulting hash in `ADMIN_PASSWORD`.
