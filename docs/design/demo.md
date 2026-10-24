# Portfolio demo design

Expose the existing payment engine through a small browser walkthrough.
Visitors should create a test payment, replay it, capture part, refund part, and inspect balanced postings.
Keep the existing Python, FastAPI, Postgres, and Redis stack.
Serve buildless HTML, CSS, and JavaScript from the same app.

## Behavior

- Demo routes exist only when `DEMO_ENABLED=true`.
- Each visitor gets an opaque HttpOnly, SameSite=Strict session cookie scoped to `/demo`.
- Store only the session token hash, merchant ID, creation time, and expiry in Postgres.
- Sessions expire after one hour. Reuse an existing valid session instead of creating another merchant.
- Create a merchant and its INR/USD accounts in one transaction when a session begins.
- Never send merchant API keys or the administrator secret to the browser.
- One session owns one manual INR 500.00 payment using a server-selected synthetic Visa card.
- The create action always uses the same merchant-scoped request key, so its repeat demonstrates replay.
- Capture INR 400.00 once, then refund INR 150.00 once, using separate stable request keys.
- Reject access to another session's payment through the existing ownership checks.
- Require a matching Origin header for every demo POST.
- Rate-limit session creation by remote address and enforce an atomic global daily session cap of 200 by default.
- Rate-limit operations per merchant. Demo limits fail closed when Redis is unavailable.
- Show current payment state, INR balance, signed ledger entries, and the last operation's HTTP result and replay flag.
- Check only this session's accounting in one repeatable-read snapshot. Do not expose global administrative reconciliation.
- Check account balances against entries, two balanced entries per succeeded transfer, and payment capture/refund postings.
- Do not accept arbitrary amounts, card details, credentials, URLs, or administrative actions from visitors.

## Hosting

Add `ENVIRONMENT=development|production`, defaulting to development.
Production rejects the development administrator key and requires a secret of at least 32 characters.
Production session cookies are Secure. Demo POST origins must use HTTPS in production.
Keep database and Redis connections private in hosting configuration.

Provide an optional `DEMO_EMBEDDED_GATEWAYS=true` mode for a small demo deployment.
It uses the existing mock gateway apps through private in-process HTTP transports.
No mock control endpoint is exposed publicly, and no extra gateway service is required.
Normal operation retains its existing external connectors.

Embedded mock state remains in memory. Restarting the app resets those mock records.
Record an app boot time and reject sessions created before that boot when embedding is enabled.
Visitors then start a new demo session. Existing ledger rows remain intact.
Use one API worker and one instance for this mode.
This deployment demonstrates synthetic transactions and does not process real payments.

Prepare a Render Blueprint using explicit free plans for the web service, Postgres, and Key Value.
Use generated administrator credentials and datastore references rather than stored secrets.
Document free-tier database expiry and sleep behavior, with links to provider documentation.
The hosting account and budget remain the user's choice. Do not create paid resources without that choice.

## Interface

Use a light blue-gray background, white working surface, navy text, blue actions, and teal balanced states.
Use a restrained humanist sans-serif font stack with tabular numerals for amounts.
Place the payment walkthrough beside the ledger so an action and its accounting effect can be compared directly.
Show real action order rather than decorative dashboard statistics.
At narrow widths, stack the walkthrough above the ledger.
Include keyboard focus, polite live status, visible error messages, and reduced-motion support.

## Scope

Keep the interface fixed to one successful manual payment flow and its replay.
Gateway outages, webhooks, real checkout, dashboards for real merchants, and schema migration tooling remain later milestones.
Update API, development, architecture, testing, and operations docs to reflect the implemented demo.
Test isolation, replay, cookie flags, expiry, CSRF, quotas, redaction, accounting failures, and production configuration.
