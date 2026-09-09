# -*- coding: utf-8 -*-
__author__ = "Paul Schifferer <dm@sweetrpg.com>"
"""Constants.
"""


BUILD_INFO_PATH = "BUILD_INFO_PATH"

# Rate-limiting environment variable names. These mirror the Go api-core
# ratelimit package so a service tuned in one language reads the same in another.
RATE_LIMIT_CHEAP = "RATE_LIMIT_CHEAP"
RATE_LIMIT_CHEAP_WINDOW_SECONDS = "RATE_LIMIT_CHEAP_WINDOW_SECONDS"
RATE_LIMIT_STANDARD = "RATE_LIMIT_STANDARD"
RATE_LIMIT_STANDARD_WINDOW_SECONDS = "RATE_LIMIT_STANDARD_WINDOW_SECONDS"

# Redis connection environment variable names.
REDIS_HOST = "REDIS_HOST"
REDIS_PORT = "REDIS_PORT"
REDIS_PASS = "REDIS_PASS"

# Error envelope codes, matching the Go api-core vo.ErrorVO values.
ERROR_RATE_LIMITED = "rate_limited"
ERROR_RATE_LIMIT_UNAVAILABLE = "rate_limit_unavailable"
