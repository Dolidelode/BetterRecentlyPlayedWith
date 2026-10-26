import pytest

from brpw import live
from brpw.db import Database, Player
from brpw.live import HOURLY_BUDGET, LIVE_MAX_AGE, LiveChecker, parse_presence
from brpw.providers import ProviderError


class Key:
    def get(self):
        return "k"


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p.db")
    yield d
    d.close()


def seed(db, n=5):
    db.merge_players([Player(f"P{i}", str(1000 + i)) for i in range(n)], "openxbl", True, now=100)


# NOTE: the REST shape of a broadcast record is NOT documented by Microsoft. These fixtures assume the field
# names of the documented XblPresenceBroadcastRecord (broadcastId, provider, viewerCount, startTime). The
# parser searches for them instead of assuming a path; real responses are verified only by running the app.
BROADCASTING = {"content": {"xuid": "1", "state": "Online", "devices": [{"titles": [{"name": "Sea of Thieves",
                "activity": {"broadcast": {"broadcastId": "B1", "provider": "Twitch", "viewerCount": 42,
                                           "startTime": "2026-10-26T20:00:00Z"}}}]}]}}
OFFLINE = {"content": {"xuid": "1", "state": "Offline", "lastSeen": {"titleName": "Online"}}}


def test_parse_presence_variants():
    p = parse_presence(BROADCASTING)
    assert p.twitch and p.viewers == 42 and p.broadcast_id == "B1" and p.started
    assert not parse_presence(OFFLINE).twitch
    other = {"content": {"state": "Online", "devices": [{"titles": [{"activity": {"broadcast": {"provider": "Mixer"}}}]}]}}
    assert not parse_presence(other).twitch
    with pytest.raises(ProviderError):
        parse_presence({"content": {"code": "SerializationError"}, "code": 400})


def test_name_alone_never_makes_someone_live(db):
    db.merge_players([Player("TwitchStreamerLive", "5")], "openxbl", True, now=100)
    ck = LiveChecker(db, Key(), http=lambda *a, **k: OFFLINE, now=Clock())
    ck.run_cycle()
    assert ck.live_now() == []


def test_confirmed_broadcast_shows_then_expires(db):
    seed(db, 1)
    clock = Clock()
    ck = LiveChecker(db, Key(), http=lambda *a, **k: BROADCASTING, now=clock)
    ck.run_cycle()
    rows = ck.live_now()
    assert len(rows) == 1 and rows[0]["viewers"] == 42
    clock.t += LIVE_MAX_AGE + 1
    assert ck.live_now() == []            # stale confirmation is never shown as live


def test_priority_cache_and_ttl(db):
    seed(db, 5)                            # list positions 0..4 = P0..P4
    clock = Clock()
    seen = []

    def http(url, data, headers, form=False):
        seen.append(url.rsplit("/", 1)[1])
        return OFFLINE
    ck = LiveChecker(db, Key(), http=http, now=clock)
    ck.run_cycle(max_requests=2)
    assert seen == ["1000", "1001"]        # most recent list positions first
    ck.run_cycle(max_requests=2)
    assert seen[2:] == ["1002", "1003"]    # cached players are not asked again
    clock.t += live.OFFLINE_TTL + 1
    ck.run_cycle(max_requests=1)
    assert len(seen) == 5 and seen[4] == "1004"   # never-checked beat stale ones


def test_hourly_budget_is_enforced(db):
    seed(db, 40)
    clock = Clock()
    calls = []
    ck = LiveChecker(db, Key(), http=lambda *a, **k: calls.append(1) or OFFLINE, now=clock)
    for _ in range(20):
        ck.run_cycle(max_requests=5)
    assert len(calls) == HOURLY_BUDGET
    clock.t += 3601
    ck.run_cycle(max_requests=5)
    assert len(calls) == HOURLY_BUDGET + 5


def test_429_pauses_and_keeps_data(db):
    seed(db, 3)
    clock = Clock()

    def limited(*a, **k):
        e = ProviderError("HTTP 429")
        e.code = 429
        raise e
    ck = LiveChecker(db, Key(), http=limited, now=clock)
    assert "pauses" in ck.run_cycle()
    assert ck.budget_left() == 0
    clock.t += 16 * 60
    assert ck.budget_left() > 0


def test_bad_key_requires_reauth(db):
    seed(db, 1)

    def denied(*a, **k):
        e = ProviderError("HTTP 401")
        e.code = 401
        raise e
    with pytest.raises(ProviderError) as ei:
        LiveChecker(db, Key(), http=denied, now=Clock()).run_cycle()
    assert ei.value.reauth


def test_coverage_counts(db):
    seed(db, 4)
    ck = LiveChecker(db, Key(), http=lambda *a, **k: OFFLINE, now=Clock())
    ck.run_cycle(max_requests=3)
    assert ck.coverage() == (3, 4)
