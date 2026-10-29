"""Agent API contract v1 (contracts/agent-api.md), checked against the reference mock of the container side."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from agent_api import MockAgentAPI

TOKENS = {"hosta": "tok-a", "hostb": "tok-b"}
TASK = {"name": "t1", "command": "true", "kind": "continuous", "instances": 1, "enabled": True}


@pytest.fixture
def api():
    with MockAgentAPI(TOKENS) as m:
        yield m


def call(api, method, path, body=None, token="tok-a"):
    """(status, json body or None, headers)."""
    req = urllib.request.Request(
        api.url + "/api/v1/agent" + path, method=method, data=None if body is None else json.dumps(body).encode()
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None, r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None, e.headers


def test_auth_401_and_403(api):
    assert call(api, "GET", "/servers/hosta/orders", token=None)[0] == 401
    assert call(api, "GET", "/servers/hosta/orders", token="wrong")[0] == 401
    assert call(api, "GET", "/servers/hostb/orders", token="tok-a")[0] == 403  # a valid token, another host's {host}


def test_orders_long_poll_and_empty_304(api):
    code, body, _ = call(api, "GET", "/servers/hosta/orders?version=0&wait=0.3")
    assert code == 304 and body is None  # nothing yet: empty body
    v = api.set_orders("hosta", [TASK], {"keep_days": 30})
    code, body, _ = call(api, "GET", "/servers/hosta/orders?version=0")
    assert code == 200 and body["version"] == v and body["tasks"][0]["name"] == "t1"
    assert call(api, "GET", f"/servers/hosta/orders?version={v}&wait=0.3")[0] == 304  # up to date

    got = {}
    t = threading.Thread(target=lambda: got.update(r=call(api, "GET", f"/servers/hosta/orders?version={v}&wait=10")))
    t.start()
    time.sleep(0.3)
    v2 = api.set_orders("hosta", [dict(TASK, instances=2)])
    t.join(5)
    assert got["r"][0] == 200 and got["r"][1]["version"] == v2 > v  # the long-poll answered on the change


def test_version_is_per_host(api):
    api.set_orders("hosta", [TASK])
    api.set_orders("hosta", [TASK])
    assert api.set_orders("hostb", [TASK]) == 1


def test_report_is_stored(api):
    rep = {
        "at": time.time(),
        "orders_version": 1,
        "tasks": [],
        "runs": [],
        "health": {"fan_control": {"running": True, "mode": "normal", "emergency": False}},
    }
    assert call(api, "POST", "/servers/hosta/report", rep)[1] == {"ok": True}
    assert api.reports["hosta"][0]["health"]["fan_control"]["emergency"] is False


def test_commands_at_most_once_and_three_result_kinds(api):
    a = api.enqueue("hosta", "task.restart", {"name": "t1"})
    b = api.enqueue("hosta", "unit.restart", {"unit": "nope"})
    c = api.enqueue("hosta", "task.run_now", {"name": "t1"})
    code, got, headers = call(api, "GET", "/servers/hosta/commands?wait=0")
    assert [x["id"] for x in got] == [a, b, c] and headers["Date"]  # oldest first; Date = the container's clock
    assert call(api, "GET", "/servers/hosta/commands?wait=0.2")[1] == []  # never handed out twice
    assert call(api, "POST", f"/servers/hosta/commands/{a}/result", {"ok": True})[0] == 200
    assert call(api, "POST", f"/servers/hosta/commands/{b}/result", {"ok": False, "refused": "not in allow-list"})[0] == 200
    assert call(api, "POST", f"/servers/hosta/commands/{c}/result", {"ok": False, "error": "boom"})[0] == 200
    assert [api.commands[x]["state"] for x in (a, b, c)] == ["done", "refused", "failed"]
    assert call(api, "POST", f"/servers/hosta/commands/{a}/result", {"ok": True})[0] == 409  # already finished


def test_commands_expire_after_10_min(api):
    old = api.enqueue("hosta", "task.restart", {"name": "t1"})
    assert api.commands[old]["expires_at"] - api.commands[old]["queued_at"] == 600
    api.skew = 601  # the container's clock moves on before the agent claims
    new = api.enqueue("hosta", "task.run_now", {"name": "t1"})
    got = call(api, "GET", "/servers/hosta/commands?wait=0")[1]
    assert [x["id"] for x in got] == [new] and api.commands[old]["state"] == "expired"  # never runs late


def test_late_result_after_expiry_is_409(api):
    c = api.enqueue("hosta", "unit.restart", {"unit": "x"})
    call(api, "GET", "/servers/hosta/commands?wait=0")
    api.skew = 601
    assert call(api, "POST", f"/servers/hosta/commands/{c}/result", {"ok": True})[0] == 409


def test_no_hardware_commands_on_this_path(api):
    """v2 (owner): fan override and the GPU power limit go through :8090, never container -> agent."""
    for cmd in ("fan.override", "gpu.limit"):
        with pytest.raises(ValueError):
            api.enqueue("hosta", cmd, {})


def test_commands_long_poll(api):
    got = {}
    t = threading.Thread(target=lambda: got.update(r=call(api, "GET", "/servers/hosta/commands?wait=10")))
    t.start()
    time.sleep(0.3)
    cid = api.enqueue("hosta", "agent.resync")
    t.join(5)
    assert [x["id"] for x in got["r"][1]] == [cid]


def test_another_hosts_command_is_invisible(api):
    api.enqueue("hostb", "task.restart", {"name": "t1"})
    assert call(api, "GET", "/servers/hosta/commands?wait=0")[1] == []


def test_queue_lease_retry_and_failure(api):
    (i1, i2) = api.add_items("vlm", [{"file": 1}, {"file": 2}], max_attempts=2)
    got = call(api, "POST", "/queues/vlm/claim", {"worker": "hosta-0", "n": 5, "lease_s": 60})[1]
    assert [x["id"] for x in got] == [i1, i2] and got[0]["attempt"] == 1
    assert call(api, "POST", "/queues/vlm/claim", {"n": 5, "lease_s": 60})[1] == []  # all leased
    assert call(api, "POST", f"/queues/items/{i1}/done", {"result": {"ok": 1}})[0] == 200
    api.skew = 61  # i2's lease runs out: back to ready, and a late finish is refused
    assert call(api, "POST", f"/queues/items/{i2}/done", {"result": {}})[0] == 409
    again = call(api, "POST", "/queues/vlm/claim", {"n": 5, "lease_s": 60})[1]
    assert [(x["id"], x["attempt"]) for x in again] == [(i2, 2)]
    assert call(api, "POST", f"/queues/items/{i2}/failed", {"error": "x", "retry": True})[0] == 200
    assert api.items[i2]["state"] == "failed"  # max_attempts reached: stays failed for a person
