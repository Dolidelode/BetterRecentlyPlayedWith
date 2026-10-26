import pytest

from brpw import live
from brpw.db import Database, Player
from brpw.live import BATCH, HOURLY_BUDGET, LIVE_MAX_AGE, STALE_AFTER, LiveChecker, parse_summary
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


def person(xuid, state="Offline", text="Offline", broadcasting=False, record=None):
    # shape verified against a real OpenXBL response: isBroadcasting (bool), presenceState, presenceText and a
    # `broadcast` field. The CONTENT of a broadcast record is assumed from Microsoft's XblPresenceBroadcastRecord
    # (provider, viewerCount, startTime, broadcastId): no real broadcaster has been seen yet.
    p = {"xuid": xuid, "presenceState": state, "presenceText": text, "isBroadcasting": broadcasting, "broadcast": record}
    return p


def summary_http(overrides=None, calls=None):
    """Fake OpenXBL: echoes one person per requested XUID; `overrides` maps xuid -> person dict."""
    overrides = overrides or {}

    def http(url, data, headers, form=False):
        xuids = url.rsplit("/", 1)[1].split(",")
        if calls is not None:
            calls.append(xuids)
        return {"content": {"people": [overrides.get(x, person(x)) for x in xuids]}}
    return http


TWITCH_REC = {"provider": "Twitch", "viewerCount": 42, "startTime": "2026-10-26T20:00:00Z", "broadcastId": "B1"}


def test_parse_summary_variants():
    out = parse_summary({"content": {"people": [
        person("1", "Online", "Sea of Thieves", True, TWITCH_REC),
        person("2", "Online", "Halo", True, None),                       # flag only: service not stated
        person("3", "Offline", "49m geleden voor het laatst gezien"),
        person("4", "Online", "X", False, {"provider": "SomethingElse", "viewerCount": 3}),
        {"xuid": "abc"}]}})
    assert set(out) == {"1", "2", "3", "4"}
    assert out["1"].broadcasting and out["1"].twitch and out["1"].viewers == 42 and out["1"].started
    assert out["2"].broadcasting and not out["2"].twitch and out["2"].provider is None
    assert not out["3"].broadcasting and out["3"].title.startswith("49m")
    assert out["4"].broadcasting and out["4"].provider == "SomethingElse" and not out["4"].twitch
    with pytest.raises(ProviderError):
        parse_summary({"content": {"code": "SerializationError"}})
    with pytest.raises(ProviderError):
        parse_summary({"content": {"raw": "<html>Bad Request - Invalid URL</html>"}})


def test_name_alone_never_makes_someone_live(db):
    db.merge_players([Player("TwitchStreamerLive", "5")], "openxbl", True, now=100)
    ck = LiveChecker(db, Key(), http=summary_http(), now=Clock())
    ck.run_cycle()
    assert ck.live_now() == []


def test_broadcast_shows_then_expires_and_flag_only_counts(db):
    seed(db, 2)
    clock = Clock()
    http = summary_http({"1000": person("1000", "Online", "SoT", True, TWITCH_REC), "1001": person("1001", "Online", "Halo", True)})
    ck = LiveChecker(db, Key(), http=http, now=clock)
    ck.run_cycle()
    rows = {r["gamertag"]: r for r in ck.live_now()}
    assert rows["P0"]["viewers"] == 42 and rows["P0"]["twitch"] == 1
    assert rows["P1"]["twitch"] == 0 and rows["P1"]["provider"] is None       # broadcasting, service not stated
    clock.t += LIVE_MAX_AGE + 1
    assert ck.live_now() == []                                                # stale confirmations are never shown


def test_twenty_players_cost_two_requests_and_fresh_ones_are_skipped(db):
    seed(db, 40)                                       # 40 in the list, scope is 20
    clock, calls = Clock(), []
    ck = LiveChecker(db, Key(), http=summary_http(calls=calls), now=clock)
    ck.run_cycle()
    assert [len(c) for c in calls] == [BATCH, BATCH]  # 20 players = 2 requests
    assert calls[0][0] == "1000" and calls[1][-1] == "1019"
    ck.run_cycle()
    assert len(calls) == 2                             # all fresh: nothing asked again
    clock.t += STALE_AFTER + 1
    ck.run_cycle()
    assert len(calls) == 4                             # stale again: asked again
    assert ck.coverage() == (20, 20)


def test_history_players_outside_the_list_are_never_checked(db):
    seed(db, 3)
    db.merge_players([Player("Fresh", "9999")], "openxbl", True, now=300)   # the old three drop out of the list
    calls = []
    LiveChecker(db, Key(), http=summary_http(calls=calls), now=Clock()).run_cycle()
    assert calls == [["9999"]]


def test_hourly_budget_is_enforced(db, monkeypatch):
    monkeypatch.setattr(live, "STALE_AFTER", 0)         # every player is always due, so only the budget limits us
    seed(db, 20)
    clock, calls = Clock(), []
    ck = LiveChecker(db, Key(), http=summary_http(calls=calls), now=clock)
    for _ in range(40):
        clock.t += 30                                    # 20 minutes in total, well inside one hour
        ck.run_cycle(max_requests=5)
    assert len(calls) == HOURLY_BUDGET and ck.budget_left() == 0
    clock.t += 3601                                      # an hour later the budget is available again
    ck.run_cycle(max_requests=5)
    assert len(calls) == HOURLY_BUDGET + 2


def test_429_pauses_and_keeps_data(db):
    seed(db, 3)
    clock = Clock()

    def limited(*a, **k):
        e = ProviderError("HTTP 429")
        e.code = 429
        raise e
    ck = LiveChecker(db, Key(), http=limited, now=clock)
    assert "pause" in ck.run_cycle()
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


def test_other_errors_do_not_stop_the_cycle_and_are_reported(db):
    seed(db, 20)
    n = []

    def flaky(url, data, headers, form=False):
        n.append(1)
        if len(n) == 1:
            raise ProviderError("temporary outage")
        return summary_http()(url, data, headers)
    ck = LiveChecker(db, Key(), http=flaky, now=Clock())
    msg = ck.run_cycle()
    assert ck.last_error == "temporary outage" and "Checked 10 player(s) in 2 request(s)" in msg


def test_check_accounts_ignores_freshness_batches_and_explains(db):
    db.merge_players([Player(f"P{i}", str(1000 + i)) for i in range(14)] + [Player("NoXuid")], "openxbl", True, now=100)
    ids = [r["id"] for r in db.list_accounts()]
    calls = []
    http = summary_http({"1000": person("1000", "Online", "SoT", True, TWITCH_REC), "1001": person("1001", "Online", "Halo", True),
                         "1002": person("1002", "Online", "X", False, {"provider": "SomethingElse"})}, calls)
    ck = LiveChecker(db, Key(), http=http, now=Clock())
    out = ck.check_accounts(ids)
    assert [len(c) for c in calls if len(c) > 1] == [10, 4]            # 14 players with an XUID = 2 summary requests
    by = {r["gamertag"]: r for r in out}
    assert by["P0"]["twitch"] and by["P0"]["broadcasting"] and by["P0"]["viewers"] == 42
    assert by["P1"]["broadcasting"] and not by["P1"]["twitch"] and by["P1"]["provider"] is None
    assert by["P2"]["provider"] == "SomethingElse" and not by["P2"]["twitch"]
    assert not by["P3"]["broadcasting"] and by["P3"]["state"] == "Offline"
    assert "XUID" in by["NoXuid"]["error"]
    n = len(calls)
    ck.check_accounts(ids[:3])                                          # asking again asks Xbox again
    assert len(calls) > n
    assert {r["gamertag"] for r in ck.live_now()} == {"P0", "P1", "P2"}


def test_check_accounts_caps_players_429_and_bad_key(db):
    seed(db, 25)
    ids = [r["id"] for r in db.list_accounts()]
    calls = []
    out = LiveChecker(db, Key(), http=summary_http(calls=calls), now=Clock()).check_accounts(ids)
    assert sum(len(c) for c in calls) == 20 and sum(1 for r in out if r["error"] and "at most 20" in r["error"]) == 5

    def limited(*a, **k):
        e = ProviderError("HTTP 429")
        e.code = 429
        raise e
    ck = LiveChecker(db, Key(), http=limited, now=Clock())
    out = ck.check_accounts(ids[:12])
    assert all("rate limit" in r["error"] for r in out) and len(ck.spent) == 1 and ck.budget_left() == 0

    def denied(*a, **k):
        e = ProviderError("HTTP 401")
        e.code = 401
        raise e
    with pytest.raises(ProviderError) as ei:
        LiveChecker(db, Key(), http=denied, now=Clock()).check_accounts(ids)
    assert ei.value.reauth
    assert LiveChecker(db, Key(), http=denied, now=Clock()).check_accounts([]) == []


PRESENCE_TWITCH = {"content": {"state": "Online", "devices": [{"titles": [{"name": "Sea of Thieves", "activity": {
    "broadcast": {"provider": "Twitch", "viewerCount": 77, "startTime": "2026-10-26T20:00:00Z", "broadcastId": "B9"}}}]}]}}


def routed_http(summary=None, presence=None, calls=None):
    """Fake OpenXBL: summary requests echo one person per XUID; single presence requests return `presence[xuid]`."""
    summary, presence = summary or {}, presence or {}

    def http(url, data, headers, form=False):
        tail = url.rsplit("/", 1)[1]
        if "/player/summary/" in url:
            xs = tail.split(",")
            if calls is not None:
                calls.append(("summary", xs))
            return {"content": {"people": [summary.get(x, person(x)) for x in xs]}}
        if calls is not None:
            calls.append(("presence", tail))
        reply = presence.get(tail)
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else {"content": {}}
    return http


def test_broadcaster_without_named_service_is_enriched_once(db):
    seed(db, 3)
    clock, calls = Clock(), []
    http = routed_http({"1001": person("1001", "Online", "SoT", True)}, {"1001": PRESENCE_TWITCH}, calls)
    ck = LiveChecker(db, Key(), http=http, now=clock)
    ck.run_cycle()
    kinds = [c[0] for c in calls]
    assert kinds == ["summary", "presence"] and calls[1][1] == "1001"      # one extra request, only for the broadcaster
    row = {r["gamertag"]: r for r in ck.live_now()}["P1"]
    assert row["provider"] == "Twitch" and row["twitch"] == 1 and row["viewers"] == 77 and row["started"]
    clock.t += STALE_AFTER + 1                                              # next cycle: still broadcasting
    ck.run_cycle()
    assert [c[0] for c in calls] == ["summary", "presence", "summary"]     # details reused, no second presence request
    row = {r["gamertag"]: r for r in ck.live_now()}["P1"]
    assert row["provider"] == "Twitch" and row["twitch"] == 1


def test_enrichment_failures_never_lose_the_summary_result(db):
    seed(db, 2)
    for failure in (ProviderError("temporary outage"), {"content": {"code": "SerializationError"}}):
        calls = []
        http = routed_http({"1000": person("1000", "Online", "SoT", True)}, {"1000": failure}, calls)
        ck = LiveChecker(db, Key(), http=http, now=Clock())
        ck.run_cycle()
        r = {x["gamertag"]: x for x in ck.live_now()}["P0"]
        assert r["twitch"] == 0 and r["provider"] is None                  # still shown as broadcasting, service not stated


def test_enrichment_429_pauses_and_bad_key_raises(db):
    seed(db, 2)

    def err(code):
        e = ProviderError(f"HTTP {code}")
        e.code = code
        return e
    clock = Clock()
    ck = LiveChecker(db, Key(), http=routed_http({"1000": person("1000", "Online", "SoT", True)}, {"1000": err(429)}),
                     now=clock)
    ck.run_cycle()
    assert ck.budget_left() == 0 and {r["gamertag"] for r in ck.live_now()} == {"P0"}
    with pytest.raises(ProviderError) as ei:
        LiveChecker(db, Key(), http=routed_http({"1000": person("1000", "Online", "SoT", True)}, {"1000": err(401)}),
                    now=Clock(1_000_000 + 3 * STALE_AFTER)).run_cycle()      # later, so the stored result is stale
    assert ei.value.reauth


def test_enrichment_respects_budget_in_cycles_but_not_in_check_live(db, monkeypatch):
    seed(db, 2)
    calls = []
    http = routed_http({"1000": person("1000", "Online", "SoT", True)}, {"1000": PRESENCE_TWITCH}, calls)
    ck = LiveChecker(db, Key(), http=http, now=Clock())
    for _ in range(HOURLY_BUDGET - 1):
        ck.spent.append(ck.now())                                          # only one request left in this hour
    ck.run_cycle()
    assert [c[0] for c in calls] == ["summary"]                            # the budget went on the summary; no presence
    out = ck.check_accounts([r["id"] for r in db.list_accounts()])         # the user pressed Check live: always allowed
    assert [c[0] for c in calls] == ["summary", "summary", "presence"]
    p0 = next(r for r in out if r["gamertag"] == "P0")
    assert p0["twitch"] and p0["viewers"] == 77 and p0["started"]


def test_parse_presence_detail():
    from brpw.live import parse_presence_detail
    p = parse_presence_detail(PRESENCE_TWITCH)
    assert p.broadcasting and p.twitch and p.viewers == 77 and p.started and p.broadcast_id == "B9"
    assert not parse_presence_detail({"content": {"state": "Offline"}}).broadcasting
    with pytest.raises(ProviderError):
        parse_presence_detail({"content": {"code": "SerializationError"}})
