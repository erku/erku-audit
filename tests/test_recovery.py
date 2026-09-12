from f916 import recovery


def test_parse_retry_after_accepts_seconds_int_and_str():
    now = 1000.0
    assert recovery.parse_retry_after(30, now) == 1030.0
    assert recovery.parse_retry_after("30", now) == 1030.0
    assert recovery.parse_retry_after("0", now) == 1000.0


def test_parse_retry_after_accepts_http_date():
    now = 1000.0
    # Sun, 06 Nov 1994 08:49:37 GMT -> well-known epoch value.
    assert recovery.parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", now) == 784111777.0


def test_parse_retry_after_returns_none_for_junk():
    now = 1000.0
    assert recovery.parse_retry_after("not-a-date-or-number", now) is None
    assert recovery.parse_retry_after(None, now) is None
    assert recovery.parse_retry_after("", now) is None


def test_backoff_seconds_monotonic_and_clamped_to_cap():
    base, cap = 60, 3600
    values = [recovery.backoff_seconds(n, base, cap) for n in range(1, 10)]
    assert values == sorted(values)
    assert values[0] == base
    assert all(v <= cap for v in values)
    assert values[-1] == cap  # exponential growth saturates well before n=9


def test_backoff_seconds_never_below_base():
    assert recovery.backoff_seconds(1, 60, 3600) == 60
    assert recovery.backoff_seconds(0, 60, 3600) == 60  # attempts clamped to >=1


def test_record_rate_limit_honours_supplied_retry_after():
    now = 1000.0
    state = recovery.record_rate_limit({}, now + 30, now, base=60, cap=3600)
    assert state["attempts"] == 1
    assert state["blocked_until"] == now + 30
    assert state["last"] == now


def test_record_rate_limit_ignores_retry_after_in_the_past():
    now = 1000.0
    state = recovery.record_rate_limit({}, now - 5, now, base=60, cap=3600)
    # Falls back to bounded backoff since the supplied time is not in the future.
    assert state["blocked_until"] > now
    assert state["blocked_until"] - now <= 3600


def test_record_rate_limit_falls_back_to_bounded_backoff_without_retry_after():
    now = 1000.0
    state = {}
    for _ in range(20):
        state = recovery.record_rate_limit(state, None, now, base=60, cap=3600)
        assert state["blocked_until"] - now <= 3600
        assert state["blocked_until"] - now >= 60
    assert state["attempts"] == 20


def test_is_blocked_before_and_after():
    now = 1000.0
    assert recovery.is_blocked({}, now) is False
    assert recovery.is_blocked({"blocked_until": now - 1}, now) is False
    assert recovery.is_blocked({"blocked_until": now + 1}, now) is True
