# Academic Ace production deployment

## Recommended payment provider

For a Zambia-first launch, this build uses Flutterwave's hosted checkout boundary. Flutterwave currently documents Zambia collections in ZMW, including local mobile-money payments and international cards. The application never handles card numbers itself; it redirects clients to the hosted checkout and verifies transactions server-side.

## Environment variables

Set:

- `SECRET_KEY` — long random secret
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` — strong password, not the development default
- `DATABASE_URL` — managed PostgreSQL connection string
- `CURRENCY=ZMW`
- `PUBLIC_BASE_URL=https://your-domain.example`
- `SESSION_COOKIE_SECURE=1`
- `FLW_SECRET_KEY`
- `FLW_WEBHOOK_SECRET`
- optional SMTP variables for notifications

## Flutterwave dashboard

Create a Flutterwave merchant account and configure:

- ZMW collection
- hosted checkout
- webhook URL: `https://your-domain.example/webhooks/flutterwave`
- webhook secret hash equal to `FLW_WEBHOOK_SECRET`

The application verifies successful transactions against the Flutterwave API before marking a milestone paid.

## Database

This release still uses SQLAlchemy `create_all()` for first deployment. For an already-running production database, apply the SQL in `migrations/001_v4.sql` or use a proper migration tool before deploying code that expects the new columns.

## Files

Do not use local ephemeral disk for production uploads. Set `UPLOAD_FOLDER` to a persistent/private volume or replace the storage adapter with private object storage (S3-compatible storage is recommended).

## Health check

`GET /health` returns a small JSON health response and should be configured as the deployment health check.

## Important

Before accepting real client documents, configure backups, malware scanning, private storage, administrator MFA/SSO, monitoring, a privacy policy, terms of service, and a tested recovery procedure.
