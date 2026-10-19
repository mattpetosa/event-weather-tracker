import pytest

from eventweather.config import Config, startup_warnings


def test_defaults():
    c = Config.from_env({})
    assert (c.host, c.port, c.cache_ttl, c.rate_limit, c.search_limit) == ("127.0.0.1", 8080, 3600, 30, 60)
    assert not c.enable_twc and not c.enable_foreca and not c.trust_proxy and c.contact is None
    assert c.source_timeout == 20.0 and c.cache_size == 500


def test_reads_every_variable():
    c = Config.from_env({
        "EWT_HOST": "0.0.0.0", "EWT_PORT": "9000", "EWT_CONTACT": "ops@example.org",
        "EWT_ENABLE_TWC": "1", "TWC_API_KEY": "k", "EWT_ENABLE_FORECA": "true",
        "EWT_CACHE_TTL": "60", "EWT_CACHE_SIZE": "10", "EWT_SOURCE_TIMEOUT": "5.5",
        "EWT_RATE_LIMIT": "3", "EWT_SEARCH_LIMIT": "7", "EWT_TRUST_PROXY": "yes",
        "EWT_RULES_FILE": "/tmp/r.json"})
    assert (c.host, c.port, c.contact, c.twc_key) == ("0.0.0.0", 9000, "ops@example.org", "k")
    assert c.enable_twc and c.enable_foreca and c.trust_proxy
    assert (c.cache_ttl, c.cache_size, c.source_timeout, c.rate_limit, c.search_limit) == (60, 10, 5.5, 3, 7)
    assert c.rules_file == "/tmp/r.json"


@pytest.mark.parametrize("env", [{"EWT_PORT": "x"}, {"EWT_PORT": "70000"}, {"EWT_ENABLE_TWC": "maybe"},
                                 {"EWT_RATE_LIMIT": "-1"}])
def test_bad_values_name_the_variable(env):
    with pytest.raises(ValueError, match=next(iter(env))):
        Config.from_env(env)


def test_user_agent_carries_the_contact():
    assert "ops@example.org" in Config(contact="ops@example.org").user_agent()
    assert Config().user_agent().startswith("event-weather-tracker/")


def test_startup_warnings():
    assert any("EWT_CONTACT" in w for w in startup_warnings(Config()))
    assert any("TWC_API_KEY" in w for w in startup_warnings(Config(contact="x", enable_twc=True)))
    assert startup_warnings(Config(contact="x")) == []
