# -*- coding: utf-8 -*-
__author__ = "Paul Schifferer <dm@sweetrpg.com>"
"""Tests for the Redis-backed per-client rate-limit middleware.
"""

import json

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from werkzeug.test import Client

from sweetrpg_api_core.middleware import RateLimitConfig, RateLimitMiddleware, Tier


def _downstream(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


def _config(cheap=Tier(2, 60), standard=Tier(3, 60), redis_host="localhost"):
    return RateLimitConfig(cheap=cheap, standard=standard, redis_host=redis_host)


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def client(redis_client):
    mw = RateLimitMiddleware(_downstream, redis_client=redis_client, config=_config())
    return Client(mw)


def _body(response):
    return json.loads(response.get_data())


def test_requests_under_the_limit_pass_through(client):
    for _ in range(3):
        resp = client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 200


def test_request_over_the_standard_limit_gets_429(client):
    for _ in range(3):
        client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})

    resp = client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})

    assert resp.status_code == 429
    assert resp.headers["Content-Type"] == "application/json"
    assert _body(resp) == {"error": "rate_limited", "message": "Limit exceeded"}


def test_cheap_tier_has_its_own_budget(client):
    for _ in range(3):
        client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
    assert client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429

    # standard tier is exhausted for this client; cheap tier is untouched.
    assert client.get("/status/ping", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 200
    assert client.get("/status/ping", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 200
    assert client.get("/status/ping", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429


def test_limit_is_per_client_ip(client):
    for _ in range(3):
        assert client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 200
    assert client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429

    # a different IP still has its full budget.
    assert client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.2"}).status_code == 200


def test_api_key_is_keyed_independently_of_ip(client, redis_client):
    for _ in range(3):
        client.get("/things", headers={"X-API-Key": "abc"}, environ_overrides={"REMOTE_ADDR": "10.0.0.1"})

    assert client.get("/things", headers={"X-API-Key": "abc"}, environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429
    # same IP, no key -> the ip: bucket, which is fresh.
    assert client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 200
    assert redis_client.exists("ratelimit:standard:key:abc")
    assert redis_client.exists("ratelimit:standard:ip:10.0.0.1")


def test_x_forwarded_for_first_hop_is_the_client(client):
    for _ in range(3):
        client.get("/things", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.9"})
    assert client.get("/things", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.9"}).status_code == 429
    # different real client behind the same proxy is unaffected.
    assert client.get("/things", headers={"X-Forwarded-For": "203.0.113.8, 10.0.0.9"}).status_code == 200


def test_expiry_is_set_on_the_first_request(client, redis_client):
    client.get("/things", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
    ttl = redis_client.ttl("ratelimit:standard:ip:10.0.0.1")
    assert 0 < ttl <= 60


def test_redis_key_format_matches_the_go_contract(redis_client):
    mw = RateLimitMiddleware(_downstream, redis_client=redis_client, config=_config())
    Client(mw).get("/things", environ_overrides={"REMOTE_ADDR": "1.2.3.4"})
    assert redis_client.keys() == [b"ratelimit:standard:ip:1.2.3.4"]


def test_fails_closed_with_503_when_redis_is_not_configured():
    mw = RateLimitMiddleware(_downstream, config=RateLimitConfig(redis_host=None))
    resp = Client(mw).get("/things")

    assert resp.status_code == 503
    assert _body(resp) == {
        "error": "rate_limit_unavailable",
        "message": "Rate limiting is temporarily unavailable",
    }


class _UnreachableRedis:
    def incr(self, *_args, **_kwargs):
        raise RedisConnectionError("connection refused")

    def expire(self, *_args, **_kwargs):  # pragma: no cover - never reached
        raise RedisConnectionError("connection refused")


def test_fails_closed_with_503_when_the_backend_is_unreachable():
    mw = RateLimitMiddleware(_downstream, redis_client=_UnreachableRedis(), config=_config())
    resp = Client(mw).get("/things")

    assert resp.status_code == 503
    assert _body(resp) == {
        "error": "rate_limit_unavailable",
        "message": "Rate limiting is temporarily unavailable",
    }


def test_from_env_reads_the_documented_variables():
    cfg = RateLimitConfig.from_env(
        {
            "RATE_LIMIT_CHEAP": "5",
            "RATE_LIMIT_CHEAP_WINDOW_SECONDS": "10",
            "RATE_LIMIT_STANDARD": "7",
            "RATE_LIMIT_STANDARD_WINDOW_SECONDS": "20",
            "REDIS_HOST": "cache",
            "REDIS_PORT": "6380",
            "REDIS_PASS": "secret",
        }
    )

    assert cfg.cheap == Tier(5, 10)
    assert cfg.standard == Tier(7, 20)
    assert cfg.redis_host == "cache"
    assert cfg.redis_port == 6380
    assert cfg.redis_pass == "secret"


def test_from_env_falls_back_to_defaults_on_junk_values():
    cfg = RateLimitConfig.from_env({"RATE_LIMIT_STANDARD": "not-a-number"})

    assert cfg.standard == Tier(30, 60)
    assert cfg.redis_host is None
