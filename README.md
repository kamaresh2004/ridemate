# RideMate

Carpooling platform — Flask + MongoDB API, vanilla-JS SPA, real-time notifications.

- **Backend**: Flask 3 app factory in `backend/` (state machines, JWT + refresh-cookie session, atomic seat booking, demo/Razorpay payments + double-entry ledger, timezone-aware scheduling, S3/local private documents, rate limiting, admin routes).
- **Frontend**: static SPA in `frontend/` (no build step — open `frontend/index.html` or serve the folder).

## Quickstart

### 1. MongoDB

Run any reachable mongod; the app pings candidates in order:

```
mongod --dbpath <data-dir> --port 27017
```

### 2. Backend

```
cd backend
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # + pytest, pyflakes, pip-audit (for tests/CI)
copy .env.example .env        # then edit secrets (or set real env vars)
python -m backend.app          # from the repo root, or: flask --app backend.app run
```

Boot-time health: `GET /api/health`, readiness `GET /api/ready`.

Seed demo data (optional): `python -m backend.seed`

**Local demo (seeded accounts, local MongoDB).** Use the `ridemate` database —
that is where the seeded accounts live, so pointing the API at a different
database name makes every demo login fail with *Invalid email or password*:

```powershell
.\start-local.ps1      # MONGO_URI=mongodb://localhost:27017, MONGO_DB_NAME=ridemate, demo payments
```

Seed accounts (`Password123`):

| Account | Use |
| --- | --- |
| `rahul@ridemate.app`, `priya@ridemate.app`, … | riders / captains |
| `maintainer@ridemate.local` | admin for the maintenance console |
| `rider.kyc.559213@ridemate.local` | rider with an approved identity |

### 3. Frontend

```
cd frontend
python -m http.server 5500     # or any static server
```

`frontend/js/api.js` reads `window.RIDEMATE_API` for a cross-origin API base
(e.g., `http://localhost:5000`); otherwise it uses the same origin. The API's
CORS allow-list is `FRONTEND_ORIGINS`.

### 4. Background jobs

Run `python -m backend.worker` as a separate process to drain settlement, KYC,
and licence OCR jobs. Local OCR requires the Tesseract executable in addition
to the pinned Python dependencies; the Docker image installs Tesseract.

## Place search

Autocomplete merges two sources:

- **Offline gazetteer** — ~556k settlements and the state/district/taluk seats
  of all 36 states and UTs (`backend/data/places_india.bin.gz`, from
  [GeoNames](https://www.geonames.org/), CC BY 4.0). Typing any Indian district
  or taluk resolves instantly, even where OpenStreetMap has no record or is
  unreachable. The file is packed as arrays rather than JSON objects so the
  full country costs ~60 MB of RAM on the 512 MB free instance.
- **Nominatim / OSRM** — POIs (bus stands, temples, hospitals) and routing.

Town names typed with common Indian spellings or typos (`bustand`, `Trichy`,
`Tanjore`, `Karaikudi` vs `Karaikkudi`) are matched server-side. Refresh the
gazetteer only when updating the data (needs internet):

```
python -m backend.tools.build_places --download
```

## Configuration

All config keys come from environment variables / `backend/.env` — see
`backend/.env.example` for the full list with commentary. Required in
production:

| Key | Purpose |
| --- | --- |
| `SECRET_KEY` | Strong, unique secret from a secret manager; placeholders fail the production boot gate |
| `JWT_SECRET` | Strong, unique secret (32+ random characters); rotate with a planned session invalidation |
| `MONGO_URI` | Managed production MongoDB/Atlas URI; production rejects localhost candidates |
| `MONGO_URI_FALLBACKS` | Only use redundant, production-grade database endpoints; never configure localhost as a production fallback |
| `PAYMENT_PROVIDER` | Must be `razorpay` in production; `demo` is development/test only |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Configure from the provider dashboard and validate with test-mode checkout before launch |
| `GOOGLE_CLIENT_ID` | Optional; enables verified Google ID-token sign-in |
| `REDIS_URL` | Required in production for distributed rate limiting and realtime fan-out; startup now fails closed if unavailable |
| `DOCUMENT_ENCRYPTION_KEY` | Required when production KYC/private-document gates are enabled; keep the key in a secret manager and back it up securely |
| `FRONTEND_ORIGINS` | Explicit HTTPS origin allow-list; wildcard origins are rejected |
| `REFRESH_COOKIE_SECURE` | Must be `true` behind TLS (enforced by the production boot gate) |
| `SSE_MAX_STREAMS` | Max concurrent live-tracking streams per worker process (default `100`); at the cap the endpoint returns `503 stream_capacity` with `Retry-After: 5` instead of starving ordinary requests |
| `GUNICORN_WORKER_CLASS`, `GUNICORN_WORKERS`, `GUNICORN_WORKER_CONNECTIONS`, `GUNICORN_TIMEOUT` | Production server tuning; defaults are `gevent`, `min(cpu_count*2+1, 6)`, `1000`, `120` (see `gunicorn.conf.py`) |

## Session & security model

- The 15-minute access JWT is returned in the login/register/refresh body and
  sent as `Authorization: Bearer`. The SPA keeps it **in memory only** (never
  `localStorage`/`sessionStorage`); a page reload restores the session by
  rotating the refresh cookie. The refresh token is an **HttpOnly,
  SameSite=Strict cookie** (`rm_refresh`), rotated on every refresh.
- The SPA silently retries a 401 by calling `POST /api/auth/refresh` exactly
  once before surfacing a session-expired logout (`frontend/js/api.js`).
- Access tokens are short-lived; nothing sensitive is kept in the cookie.
- Input validation is server-side-only (password policy, age, rate, geo,
  document mime/size, allowed rides, payment amounts are recomputed, not trusted
  from the client).
- Rate limits: strict buckets for auth, booking mutations, refresh, and
  payments; per-route defaults from config, in-process buckets, or Redis.
- Private documents (RC/DL/insurance) are served **only** through the authorized
  endpoint `GET /api/uploads/vehicle-doc/<vehicle_id>/<kind>` (owner/admin); the
  public `/uploads/<path>` route refuses them. Avatars are the only public files.

## Testing

The current suite has **452 tests** (0 skips), all passing.

```
python -m pytest backend/tests -q     # uses the ridemate_test DB (cleaned per test)
# pyflakes on production modules (the same check CI runs; tests excluded)
find backend -name '*.py' -not -path 'backend/tests/*' -print0 | xargs -0 python -m pyflakes
python -m pip_audit -r backend/requirements.txt
python ...\smoke_hardened.py          # end-to-end black-box smoke against a live server
```

The pytest suite covers auth (rotation, logout-all, password change, fake-Google
rejection, anti-enumeration, rate limiting), bookings (full lifecycle, refunds,
two-phase concurrency with 100 distinct riders, idempotency), timezone (IST/DST/
midnight round-trips), uploads (mime/owner IDOR), document & profile privacy,
search radius/visibility, and payments/ledger integrity. It has caught real
production bugs — e.g., the unique-but-null `idempotency_key` index that silently
allowed only one booking per ride (see `backend/db.py:_unique_string_index`).

## Deployment

- WSGI entry point: `backend/wsgi.py` (`application`).
  `gunicorn -c gunicorn.conf.py backend.wsgi:application`
  The config defaults to the **gevent** worker: live-tracking SSE streams are
  greenlets, not request threads, and `GUNICORN_WORKER_CONNECTIONS=1000` gives
  each worker room for many concurrent clients. `SSE_MAX_STREAMS` (default 100
  per process) bounds streams and answers overflow with a retryable
  `503 stream_capacity`. Run `gunicorn --check-config -c gunicorn.conf.py ...`
  after changing these values.
- Container: `docker build -t ridemate . && docker run -p 5000:5000 ridemate`
  (`backend/.env`, uploads, and logs are excluded from the image — see
  `.dockerignore`).
- The DB indexes (including partial unique indexes) are created automatically at
  app boot (`backend/db.py:create_indexes`). In production a failed unique or
  `2dsphere` index aborts the boot instead of running with weaker guarantees;
  performance-only index failures are logged with collection/index/error and
  stay best-effort.
- Multiple workers/shared rate limits require `REDIS_URL`; the demo payment
  provider is single-process-safe and for development only.