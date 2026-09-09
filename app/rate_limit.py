from slowapi import Limiter
from slowapi.util import get_remote_address

# Shared instance, in its own module rather than app/main.py, so routers
# can import it without a circular import (app/main.py is what imports
# every router in the first place). In-memory storage (slowapi's default)
# is the right call here - this app runs as a single Render instance with
# no Redis, matching the same "no extra infrastructure" precedent already
# set by the lazy trash/orphan purges and the live dashboard nudge query
# instead of a real scheduled job.

# headers_enabled=True: without it, slowapi's _inject_headers (called from
# our custom 429 handler in app/main.py) silently no-ops - it checks
# self._headers_enabled before setting anything, and the library defaults
# that to False. This is what actually makes Retry-After/X-RateLimit-*
# appear on responses.
limiter = Limiter(key_func=get_remote_address, headers_enabled=True)
