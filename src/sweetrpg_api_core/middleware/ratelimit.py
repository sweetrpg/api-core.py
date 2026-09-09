# -*- coding: utf-8 -*-
__author__ = "Paul Schifferer <dm@sweetrpg.com>"
"""Redis-backed per-client/IP request rate limiting.

Framework-agnostic WSGI middleware that mirrors the shared Go implementation in
``api-core/go/ratelimit``:

* one counter per client per route tier in Redis (``INCR`` then ``EXPIRE``), so the
  limit is consistent across replicas instead of per-process,
* two tiers - ``/status/*`` is ``cheap``, everything else is ``standard`` - each with
  its own budget,
* fail closed: a 503 when the Redis backend is unreachable or was never configured,
  never unlimited traffic,
* a 429 with the api-core error envelope (``{"error": ..., "message": ...}``) on
  exceed.

Usage with Flask::

    from sweetrpg_api_core.middleware import RateLimitMiddleware

    app.wsgi_app = RateLimitMiddleware(app.wsgi_app)

``RateLimitConfig.from_env`` reads ``RATE_LIMIT_CHEAP``,
``RATE_LIMIT_CHEAP_WINDOW_SECONDS``, ``RATE_LIMIT_STANDARD``,
``RATE_LIMIT_STANDARD_WINDOW_SECONDS`` and ``REDIS_HOST``/``REDIS_PORT``/``REDIS_PASS``.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from sweetrpg_api_core import constants


__all__ = ["Tier", "RateLimitConfig", "RateLimitMiddleware"]

_logger = logging.getLogger(__name__)

_CHEAP_TIER = "cheap"
_STANDARD_TIER = "standard"
_STATUS_PREFIX = "/status/"

# Defaults match the Go api-core ratelimit package so behavior is identical when a
# service sets no environment overrides.
_DEFAULT_CHEAP_LIMIT = 120
_DEFAULT_CHEAP_WINDOW = 60
_DEFAULT_STANDARD_LIMIT = 30
_DEFAULT_STANDARD_WINDOW = 60

_DEFAULT_REDIS_PORT = 6379

_STATUS_TEXT = {429: "Too Many Requests", 503: "Service Unavailable"}

try:  # redis is a hard dependency, but stay importable if it is somehow absent.
    from redis import RedisError

    _REDIS_ERRORS: tuple = (RedisError, OSError)
except ImportError:  # pragma: no cover - redis is declared in setup.py
    _REDIS_ERRORS = (OSError,)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Return ``env[name]`` as an int, or ``default`` when unset or unparseable."""
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        _logger.warning("rate-limit: %s=%r is not an integer; using default %d", name, raw, default)
        return default


def _client_ip(environ: Mapping[str, str]) -> str:
    """Best-effort client IP: first ``X-Forwarded-For`` hop, then ``X-Real-IP``, then
    ``REMOTE_ADDR``. Services run behind Traefik, so ``REMOTE_ADDR`` alone would key
    every caller to the proxy."""
    forwarded = environ.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return environ.get("HTTP_X_REAL_IP") or environ.get("REMOTE_ADDR") or "unknown"


def _client_key(environ: Mapping[str, str]) -> str:
    """Identify the caller: ``key:<X-API-Key>`` if the header is present, else
    ``ip:<client ip>``. Prefixes match the Go implementation."""
    api_key = environ.get("HTTP_X_API_KEY")
    if api_key:
        return "key:" + api_key
    return "ip:" + _client_ip(environ)


def _default_error_body(error: str, message: str) -> dict:
    """Build the api-core error envelope, matching the Go ``vo.ErrorVO`` shape."""
    return {"error": error, "message": message}


@dataclass(frozen=True)
class Tier:
    """A rate-limit budget: at most ``limit`` requests per ``window`` seconds."""

    limit: int
    window: int


@dataclass(frozen=True)
class RateLimitConfig:
    """Tier budgets and Redis connection settings for :class:`RateLimitMiddleware`."""

    cheap: Tier = field(default_factory=lambda: Tier(_DEFAULT_CHEAP_LIMIT, _DEFAULT_CHEAP_WINDOW))
    standard: Tier = field(default_factory=lambda: Tier(_DEFAULT_STANDARD_LIMIT, _DEFAULT_STANDARD_WINDOW))
    redis_host: Optional[str] = None
    redis_port: int = _DEFAULT_REDIS_PORT
    redis_pass: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "RateLimitConfig":
        """Build a config from environment variables (``os.environ`` by default)."""
        env = environ if environ is not None else os.environ
        return cls(
            cheap=Tier(
                _env_int(env, constants.RATE_LIMIT_CHEAP, _DEFAULT_CHEAP_LIMIT),
                _env_int(env, constants.RATE_LIMIT_CHEAP_WINDOW_SECONDS, _DEFAULT_CHEAP_WINDOW),
            ),
            standard=Tier(
                _env_int(env, constants.RATE_LIMIT_STANDARD, _DEFAULT_STANDARD_LIMIT),
                _env_int(env, constants.RATE_LIMIT_STANDARD_WINDOW_SECONDS, _DEFAULT_STANDARD_WINDOW),
            ),
            redis_host=env.get(constants.REDIS_HOST) or None,
            redis_port=_env_int(env, constants.REDIS_PORT, _DEFAULT_REDIS_PORT),
            redis_pass=env.get(constants.REDIS_PASS) or None,
        )


def _build_client(config: RateLimitConfig):
    """Construct a redis client from ``config``, or ``None`` when no host is set."""
    if not config.redis_host:
        return None
    try:
        import redis
    except ImportError:  # pragma: no cover - redis is declared in setup.py
        _logger.error("rate-limit: redis package not installed; requests will fail closed")
        return None
    return redis.Redis(
        host=config.redis_host,
        port=config.redis_port,
        password=config.redis_pass,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=False,
    )


class RateLimitMiddleware:
    """WSGI middleware enforcing per-client, per-tier request limits via Redis.

    :param app: the wrapped WSGI application.
    :param redis_client: a redis client (anything exposing ``incr``/``expire``). When
        omitted, one is built from ``config`` if ``redis_host`` is set, else the
        middleware fails closed on every limited request.
    :param config: tier budgets and Redis settings; defaults to
        :meth:`RateLimitConfig.from_env`.
    :param error_body: callable ``(error, message) -> dict`` building the response
        body; defaults to the api-core ``{"error", "message"}`` envelope.
    """

    def __init__(
        self,
        app: Callable,
        redis_client=None,
        config: Optional[RateLimitConfig] = None,
        error_body: Optional[Callable[[str, str], dict]] = None,
    ) -> None:
        self.app = app
        self.config = config or RateLimitConfig.from_env()
        self._error_body = error_body or _default_error_body
        self._client = redis_client if redis_client is not None else _build_client(self.config)


    def __call__(self, environ, start_response):
        """Apply the limit, then delegate to the wrapped app when within budget."""
        path = environ.get("PATH_INFO", "") or ""
        tier_name, tier = self._tier_for(path)
        client_key = _client_key(environ)
        redis_key = "ratelimit:{}:{}".format(tier_name, client_key)

        if self._client is None:
            _logger.error("rate-limit: backend not configured; rejecting %s (fail closed)", path)
            return self._reject(
                start_response, 503, constants.ERROR_RATE_LIMIT_UNAVAILABLE,
                "Rate limiting is temporarily unavailable",
            )

        try:
            count = int(self._client.incr(redis_key))
            if count == 1:
                self._client.expire(redis_key, tier.window)
        except _REDIS_ERRORS as exc:
            _logger.error("rate-limit: backend unreachable; rejecting %s (fail closed): %s", path, exc)
            return self._reject(
                start_response, 503, constants.ERROR_RATE_LIMIT_UNAVAILABLE,
                "Rate limiting is temporarily unavailable",
            )

        if count > tier.limit:
            _logger.warning(
                "rate-limit: exceeded client=%s tier=%s path=%s (%d/%d)",
                client_key, tier_name, path, count, tier.limit,
            )
            return self._reject(start_response, 429, constants.ERROR_RATE_LIMITED, "Limit exceeded")

        return self.app(environ, start_response)


    def _tier_for(self, path: str):
        """Return ``(tier name, Tier)`` for ``path``: cheap for ``/status/*``."""
        if path.startswith(_STATUS_PREFIX):
            return _CHEAP_TIER, self.config.cheap
        return _STANDARD_TIER, self.config.standard


    def _reject(self, start_response, status_code: int, error: str, message: str):
        """Emit a JSON error response and end the request."""
        body = json.dumps(self._error_body(error, message)).encode("utf-8")
        status_line = "{} {}".format(status_code, _STATUS_TEXT[status_code])
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]
        start_response(status_line, headers)
        return [body]
