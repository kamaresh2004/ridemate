# RideMate production operations runbook

This runbook is an operational checklist, not proof that external services are configured. Keep a dated record for every drill and incident. Never paste API keys, service-account JSON, customer OTPs, identity-document contents, or payment credentials into issues or chat.

## Release gate

Do not enable public signups or accept live payments until every critical item in the production checklist has evidence. A green CI run does not prove live SMS, payment settlement, identity review, backups, legal readiness, or operator coverage.

For every release, record:
- commit SHA, CI run URL, Render deploy ID, deployment timestamp, and rollback target;
- staging smoke-test result for signup/login, ride publish/search, booking, payment callback/webhook, refunds, and KYC gates;
- secret-scan result and dependency-check result;
- name/role of the release approver and any explicitly accepted non-critical risks.

## Payout operations — manual pilot

The current application treats a ledger payable as an accounting liability, not proof of a bank transfer. Do not manually edit payout/payment rows or mark a payout paid just because someone clicked “send”.

Before the pilot:
1. The owner must approve a written manual-payout policy and identify two distinct people: preparer and approver.
2. Reconcile the driver's payable balance against completed rides, refunds, disputes, and the payout ledger.
3. The preparer records the intended driver, amount, currency, bank-account verification status, and a unique payout reference in the approved internal record.
4. A second person independently checks the recipient and amount before the transfer is initiated.
5. Record the bank/provider transaction reference, date, status evidence, and reconciliation result. Never store full bank credentials in RideMate notes.
6. If the transfer is pending, reversed, or ambiguous, stop and reconcile with the bank/provider. Do not issue a duplicate transfer.
7. The owner signs off the reconciliation before the payout is considered operationally complete.

**Implementation gate:** the backend's paid state is intended to require provider-confirmed evidence. If the selected manual process cannot produce a trusted confirmation through the existing workflow, manual payouts are not ready for production; implement and test an explicit dual-approval manual-payout state machine before use. Do not bypass this invariant.

## Identity and vehicle review

- Appoint trained reviewers using individual accounts with the admin role; never share administrator credentials.
- Review DL/insurance and RC as separate queues. Compare the applicant's name and document identifiers, validity dates, vehicle class, and registration plate. Use only documents necessary for the review.
- Approve only when the packet is legible and consistent. Reject with a factual reason; provide an appeal path and second-person escalation for suspicious or disputed documents.
- Keep an audit trail of reviewer, timestamp, decision, and reason. Restrict document downloads to authorized reviewers.
- Never claim government-record verification unless an authorized integration actually performs it.
- Follow the approved retention schedule; delete documents when the approved purpose and retention period end.

## Automated document checks (KYC provider)

Automated checks sit in front of manual review. They never replace it: a human still approves unless `KYC_AUTO_APPROVE=1`, and a failed or inconclusive check is never auto-rejected.

**Switching providers.** Configuration is environment-only.
1. Set `KYC_PROVIDER=signzy` (or `mock` in development/tests only), `KYC_PROVIDER_BASE_URL`, `KYC_PROVIDER_API_KEY`, `KYC_PROVIDER_API_SECRET`, `KYC_PROVIDER_WEBHOOK_SECRET`, and the three `KYC_PROVIDER_*_PATH` values.
2. Confirm the endpoint paths and the webhook envelope against the provider's documentation. The code marks every unverified path/field as `TODO` on purpose; do not guess them.
3. Restart the web API and the worker. `validate_runtime` refuses a production boot with an unknown provider, `mock`, missing credentials, or `KYC_AUTO_APPROVE=1` while the provider is `manual`.
4. Submit one test packet and confirm: the vehicle gets `auto_check`, the driver sees a status and reasons, and the admin queue shows the auto-check result.
5. `KYC_AUTO_APPROVE` stays `0` until reviewers agree the automated results are reliable.

**Local walkthrough (mock provider, never production).** `KYC_PROVIDER=mock` answers from the submitted details, so the whole flow can be exercised offline. In Windows PowerShell, from the repo root:
1. Add `KYC_PROVIDER=mock` and `KYC_AUTO_APPROVE=0` to `backend/.env`, then run `.venv\Scripts\python.exe -m backend.app` and, in a second window, `.venv\Scripts\python.exe -m backend.worker` (or call the KYC pass once, as the script below does).
2. Sign in at `http://localhost:5500`, open **Captain verification**, accept the consent card, add a vehicle, upload a licence and insurance image, then submit.
3. Run `.venv\Scripts\python.exe scripts\kyc_mock_demo.py --outcome pass` (also `expired`, `name`, `class`, `inconclusive`, `error`). The script logs in, records consent, creates a vehicle, seeds the licence fields the public API does not accept, submits, runs one worker pass, prints `auto_check` and the publish-gate response, then deletes the vehicle unless `--keep` is passed. Use `--base http://127.0.0.1:5055` to target another port.
4. Restore `KYC_PROVIDER=manual` when finished so the local instance matches production again.

**Reading a failed check.** On the admin review card, the auto-check badge is one of `passed`, `found issues`, `inconclusive`, or `unavailable`. The reasons underneath are the driver-facing fix-it strings ("Licence expired", "Name does not match profile", "Licence class does not cover this vehicle"). The audit log records `identity.kyc.auto_check` with the provider, provider reference, check map, attempts, and whether it auto-approved. Document numbers are never written to the audit row or the logs.

**Provider downtime.** A timeout, 5xx, 429, or malformed response is a retry, never a rejection. The worker retries with exponential backoff up to `KYC_PROVIDER_MAX_ATTEMPTS` (default 3, alias `KYC_MAX_ATTEMPTS`), then marks `auto_check.status=unavailable` and leaves the packet in the manual queue with "Manual review needed". Check `backend.worker` logs for retry lines and confirm the queue is still moving. Raising `KYC_RETRY_BASE_SECONDS` slows retries; do not raise `KYC_PROVIDER_MAX_ATTEMPTS` to hide a persistent outage.

**Local licence OCR.** The short photo precheck uses temporary files and returns reason codes only; it does not persist or log the image. Uploaded licence OCR is queued for the separate `backend.worker` process, which stores only the result state, reasons, and timestamp for the admin review card. The OCR result is advisory and never replaces an admin decision. The Docker image installs Tesseract; non-Docker worker environments need both the Python OCR requirements and the Tesseract executable installed.

**Webhook.** `POST /api/kyc/webhook` is authenticated by HMAC (`KYC_PROVIDER_WEBHOOK_SECRET`) and needs no login cookie. A bad signature is `401 invalid_signature`; an event outside `KYC_WEBHOOK_MAX_AGE_SECONDS` is `401 stale_webhook`; a repeat of the provider's event id returns `200 {"duplicate": true}` and is not reprocessed. If callbacks stop arriving, automated results still land via the worker pull path, so manual review is never blocked.

**Rollback.** Set `KYC_PROVIDER=manual` and restart web + worker. No jobs are created, no outbound lookup happens, and the original submit -> human review -> verified/rejected flow resumes immediately. Existing `auto_check` values remain visible to reviewers as history; they grant nothing on their own. Set `KYC_AUTO_APPROVE=0` as well if it was enabled, so a future provider switch cannot auto-approve unexpectedly. Revoke the provider keys at the provider console once the rollback is confirmed.

## Backup and restore drill

Before public launch, configure managed database backups and record retention settings in the provider console. Then:
1. Record the backup identifier and timestamp.
2. Restore it to an isolated staging database, never over production.
3. Verify collections/indexes, user/ride/booking/payment relationships, KYC metadata, and audit records.
4. Run backend tests and staging smoke tests against the restored copy.
5. Record duration, failures, and corrective actions. Repeat on a defined schedule and after material database changes.

A backup is not considered verified until a restore drill succeeds.

## Monitoring and incident response

The owner must fill in real, monitored contacts before launch:
- On-call owner: REQUIRED
- Passenger/driver support contact: REQUIRED
- Safety/SOS escalation contact and coverage hours: REQUIRED
- Payment/refund escalation owner: REQUIRED
- Hosting/database escalation owner: REQUIRED

Monitor at minimum: /api/ready, elevated 5xx/latency, failed logins and rate limits, Firebase SMS/auth failures, payment/webhook signature failures, webhook retry/duplicate counts, refund failures, payout reconciliation differences, document-access denials, and database/Redis availability. Alerts must reach a human who is actually on call.

When an incident occurs:
1. Protect people first. For immediate danger, direct affected people to local emergency services; do not promise that an app notification is emergency response.
2. Suspend affected accounts, rides, payment paths, or document access when needed to contain harm.
3. Preserve relevant audit/event IDs and timestamps; avoid copying identity documents or secrets into incident channels.
4. Assign an incident lead, severity, and next update time. Notify affected users and authorities when required by the approved legal process.
5. For a suspected credential leak, revoke/rotate the credential in its provider console, update the secret manager, redeploy, and audit use since the suspected exposure. A Git deletion alone does not revoke a secret.
6. Record root cause, user impact, remediation, and a post-incident review. Test rollback and recovery procedures before reopening the affected flow.

## Legal and privacy sign-off

Before launch, qualified local counsel and an insurance adviser must review the actual operating geography and business model, including applicable transport/aggregator rules, driver/passenger terms, insurance, privacy and retention, consumer protection, cancellation/refunds, grievance handling, and safety response. Record the reviewer, date, scope, and open conditions. This template is not legal advice and does not substitute for that review.
