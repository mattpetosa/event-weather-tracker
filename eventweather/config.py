"""Settings, read once from environment variables at startup."""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass

PROJECT_URL = "https://github.com/mattpetosa/event-weather-tracker"
VERSION = "1.0"


def _bool(env, name, default=False):
    v = env.get(name)
    if v is None or v == "":
        return default
    if v.lower() in ("1", "true", "yes", "on"):
        return True
    if v.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be 1 or 0, not {v!r}")


def _num(env, name, default, kind=int, minimum=0, maximum=None):
    v = env.get(name)
    if v is None or v == "":
        return default
    try:
        n = kind(v)
    except ValueError:
        raise ValueError(f"{name} must be a number, not {v!r}") from None
    if n < minimum or (maximum is not None and n > maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}" if maximum is not None
                         else f"{name} must be at least {minimum}")
    return n


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    contact: str | None = None
    enable_twc: bool = False
    twc_key: str | None = None
    enable_foreca: bool = False
    cache_ttl: int = 3600
    cache_size: int = 500
    source_timeout: float = 20.0
    rate_limit: int = 30
    search_limit: int = 60
    trust_proxy: bool = False
    rules_file: str | None = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(
            host=env.get("EWT_HOST") or "127.0.0.1",
            port=_num(env, "EWT_PORT", 8080, minimum=1, maximum=65535),
            contact=env.get("EWT_CONTACT") or None,
            enable_twc=_bool(env, "EWT_ENABLE_TWC"),
            twc_key=env.get("TWC_API_KEY") or None,
            enable_foreca=_bool(env, "EWT_ENABLE_FORECA"),
            cache_ttl=_num(env, "EWT_CACHE_TTL", 3600),
            cache_size=_num(env, "EWT_CACHE_SIZE", 500, minimum=1),
            source_timeout=_num(env, "EWT_SOURCE_TIMEOUT", 20.0, kind=float, minimum=1),
            rate_limit=_num(env, "EWT_RATE_LIMIT", 30),
            search_limit=_num(env, "EWT_SEARCH_LIMIT", 60),
            trust_proxy=_bool(env, "EWT_TRUST_PROXY"),
            rules_file=env.get("EWT_RULES_FILE") or None,
        )

    def user_agent(self):
        base = f"event-weather-tracker/{VERSION} (+{PROJECT_URL}"
        return base + (f"; {self.contact})" if self.contact else ")")


def startup_warnings(cfg):
    out = []
    if not cfg.contact:
        out.append("EWT_CONTACT is not set. NWS and MET Norway ask every app to identify "
                   "itself with a contact (an email or URL); set it so they can reach you.")
    if cfg.enable_twc and not cfg.twc_key:
        out.append("EWT_ENABLE_TWC is on but TWC_API_KEY is empty, so weather.com stays off.")
    if cfg.enable_foreca and importlib.util.find_spec("curl_cffi") is None:
        out.append("EWT_ENABLE_FORECA is on but curl_cffi is not installed "
                   "(pip install -r requirements-foreca.txt), so Foreca stays off.")
    return out
