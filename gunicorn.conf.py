"""Gunicorn production configuration.

Run from the repo root:  gunicorn -c gunicorn.conf.py backend.wsgi:application

The default worker is gevent: live tracking holds long-lived SSE responses, and
a synchronous worker spends one whole thread on each of them. A handful of
viewers would then block every ordinary API request. gevent turns each stream
into a cheap greenlet instead, so the remaining capacity keeps serving traffic.
A per-process stream cap (SSE_MAX_STREAMS, enforced in realtime.py) still bounds
resource use and answers the overflow with a retryable 503.
"""

import multiprocessing
import os
import tempfile

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
# Each gevent worker is one process; more workers are not free. The default
# stays deliberately small and is overridable for a bigger host.
workers = int(os.environ.get("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 6)))

# `gevent` (default) or `gthread`/`sync` when explicitly overridden. gevent is
# what keeps SSE streams from consuming a request thread apiece.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gevent")
# Concurrent greenlets per worker for gevent (ignored by sync/gthread).
worker_connections = int(os.environ.get("GUNICORN_WORKER_CONNECTIONS", 1000))
# Only gthread uses threads; gevent multiplexes greenlets, not OS threads.
threads = int(os.environ.get("GUNICORN_THREADS", 2)) if worker_class == "gthread" else 1

# gevent still needs an initialised hub in each process; gunicorn's gevent
# worker patches the standard library before importing the app (preload must
# therefore stay off so the patch happens before backend.app is imported).
preload_app = False

worker_tmp_dir = tempfile.mkdtemp(prefix="gunicorn-")

# SSE streams are long-lived: a generous timeout avoids killing them.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = 5
max_requests = 2000
max_requests_jitter = 200

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
capture_output = True

forwarded_allow_ips = "*"  # trusts X-Forwarded-For from the reverse proxy only
proxy_protocol = False

# Security: run as a non-privileged user when available (container).
try:
    import pwd

    _user = pwd.getpwnam("nobody")
    _uid = _user.pw_uid
    _gid = _user.pw_gid
except Exception:  # noqa: BLE001 - Windows/local dev keeps current user
    _uid = _gid = None
if _uid is not None and os.geteuid and os.geteuid() == 0:
    user = _uid
    group = _gid