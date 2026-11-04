"""Per-application authentication budgets, separate from global DSM concurrency."""

import time
from collections import OrderedDict
from hashlib import sha256

from fastapi import HTTPException

WINDOW_SECONDS = 60
LOGIN_CLIENT_LIMIT = 60
LOGIN_ACCOUNT_LIMIT = 10
BOOTSTRAP_CLIENT_LIMIT = 30
MAX_KEYS_PER_APPLICATION = 1024


class AuthQuota:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.applications = {}

    def consume(self, app_id, rules):
        # Called synchronously on the API event loop: check and increment all
        # dimensions together before the first await, without a partial charge.
        now = self.clock()
        counters = self.applications.setdefault(app_id, OrderedDict())
        while counters and next(iter(counters.values()))[0] + WINDOW_SECONDS <= now:
            counters.popitem(last=False)
        if any(counters.get(key, (now, 0))[1] >= limit for key, limit in rules):
            self.reject()
        if len(counters) + sum(key not in counters for key, _ in rules) > MAX_KEYS_PER_APPLICATION:
            # Never evict an active account budget; another app has its own cap.
            self.reject()
        for key, _ in rules:
            start, count = counters.get(key, (now, 0))
            counters[key] = (start, count + 1)

    @staticmethod
    def reject():
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(WINDOW_SECONDS)})

    def login(self, app_id, client, account):
        fingerprint = sha256(account.strip().casefold().encode()).hexdigest()
        self.consume(
            app_id,
            [
                (("login-client", client), LOGIN_CLIENT_LIMIT),
                (("login-account", fingerprint), LOGIN_ACCOUNT_LIMIT),
            ],
        )

    def bootstrap(self, app_id, client):
        self.consume(app_id, [(("bootstrap-client", client), BOOTSTRAP_CLIENT_LIMIT)])
