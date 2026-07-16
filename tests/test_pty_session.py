import asyncio
import threading
import time

import pytest

from hermes_cli.pty_session import RingBuffer, resolve_registry_settings


def test_registry_settings_preserve_existing_defaults():
    assert resolve_registry_settings({}) == (1800.0, 16)


def test_registry_settings_accept_dashboard_overrides():
    cfg = {
        "dashboard": {
            "pty_keepalive_ttl_seconds": 300,
            "pty_max_sessions": 4,
        }
    }

    assert resolve_registry_settings(cfg) == (300.0, 4)


def test_registry_settings_reject_invalid_values_without_removing_guardrails():
    cfg = {
        "dashboard": {
            "pty_keepalive_ttl_seconds": -1,
            "pty_max_sessions": 0,
        }
    }

    assert resolve_registry_settings(cfg) == (1800.0, 16)


def test_registry_settings_reject_boolean_values():
    cfg = {
        "dashboard": {
            "pty_keepalive_ttl_seconds": True,
            "pty_max_sessions": False,
        }
    }

    assert resolve_registry_settings(cfg) == (1800.0, 16)


def test_ringbuffer_keeps_everything_under_capacity():
    rb = RingBuffer(10)
    rb.append(b"abc")
    rb.append(b"def")
    assert rb.snapshot() == b"abcdef"
    assert rb.truncated is False


def test_ringbuffer_drops_oldest_over_capacity():
    rb = RingBuffer(4)
    rb.append(b"abcdef")          # 6 bytes into a 4-byte buffer
    assert rb.snapshot() == b"cdef"
    assert rb.truncated is True


def test_ringbuffer_truncation_across_appends():
    rb = RingBuffer(3)
    rb.append(b"ab")
    rb.append(b"cd")             # now "abcd" -> keep "bcd"
    assert rb.snapshot() == b"bcd"
    assert rb.truncated is True


class FakeBridge:
    """Implements the bridge contract PtySession depends on."""

    def __init__(self, chunks):
        self._chunks = list(chunks)   # bytes; b"" = idle tick; None = EOF
        self.written = bytearray()
        self.closed = False
        self.resized = None

    def read(self, timeout):
        if not self._chunks:
            return b""                # idle
        return self._chunks.pop(0)

    def write(self, data):
        self.written.extend(data)

    def resize(self, cols, rows):
        self.resized = (cols, rows)

    def close(self):
        self.closed = True


class FakeWS:
    def __init__(self):
        self.sent = []               # list of ("bytes"|"text", payload)
        self.close_code = None

    async def send_bytes(self, data):
        self.sent.append(("bytes", bytes(data)))

    async def send_text(self, text):
        self.sent.append(("text", text))

    async def close(self, code=1000, reason=""):
        self.close_code = code


@pytest.mark.asyncio
async def test_attach_replays_buffer_then_streams_live():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"hello ", b"world", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    await asyncio.sleep(0.05)                      # drain consumes "hello world"
    ws = FakeWS()
    await s.attach(ws)
    replay = b"".join(p for kind, p in ws.sent if kind == "bytes")
    assert replay == b"hello world"
    await s.close()


@pytest.mark.asyncio
async def test_detach_keeps_draining_into_buffer():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"one", b"", b"two"])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    ws = FakeWS()
    await s.attach(ws)
    s.detach(ws)
    assert s.attached is False
    assert s.last_detached_at is not None
    await asyncio.sleep(0.05)                      # "two" drains while detached
    ws2 = FakeWS()
    await s.attach(ws2)
    replay = b"".join(p for kind, p in ws2.sent if kind == "bytes")
    assert replay == b"onetwo"
    await s.close()


@pytest.mark.asyncio
async def test_eof_marks_dead_and_closes_socket_4410():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"bye", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    ws = FakeWS()
    await s.attach(ws)
    await asyncio.sleep(0.05)                      # drain hits None (EOF)
    assert s.alive is False
    assert ws.close_code == 4410
    await s.close()


from hermes_cli.pty_session import (
    PtySession,
    PtySessionRegistry,
    RegistryFull,
    RetiredToken,
)


def make_registry(ttl=1800.0, max_sessions=16):
    return PtySessionRegistry(ttl=ttl, max_sessions=max_sessions,
                              buffer_cap=1024, read_timeout=0.01)


@pytest.mark.asyncio
async def test_same_key_reattaches_same_session():
    reg = make_registry()
    b1 = FakeBridge([b"", b"", b""])
    s1, created1 = await reg.attach_or_spawn("tok", spawn=lambda: b1)
    s2, created2 = await reg.attach_or_spawn("tok", spawn=lambda: FakeBridge([]))
    assert created1 is True and created2 is False
    assert s1 is s2
    assert s2.bridge is b1                     # second spawn callable was NOT used
    await reg.close_all()


@pytest.mark.asyncio
async def test_concurrent_same_key_spawns_only_once():
    reg = make_registry()
    spawned = []

    def spawn():
        bridge = FakeBridge([b"", b""])
        spawned.append(bridge)
        time.sleep(0.05)
        return bridge

    (s1, created1), (s2, created2) = await asyncio.gather(
        reg.attach_or_spawn("tok", spawn=spawn),
        reg.attach_or_spawn("tok", spawn=spawn),
    )

    assert len(spawned) == 1
    assert s1 is s2
    assert sorted((created1, created2)) == [False, True]
    await reg.close_all()


@pytest.mark.asyncio
async def test_concurrent_new_keys_respect_capacity():
    reg = make_registry(max_sessions=1)

    def spawn():
        time.sleep(0.05)
        return FakeBridge([b"", b""])

    results = await asyncio.gather(
        reg.attach_or_spawn("a", spawn=spawn),
        reg.attach_or_spawn("b", spawn=spawn),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RegistryFull) for result in results) == 1
    assert len(reg._sessions) == 1
    await reg.close_all()


@pytest.mark.asyncio
async def test_close_key_supersedes_attached_session():
    reg = make_registry()
    bridge = FakeBridge([b"", b""])
    session, _ = await reg.attach_or_spawn("old", spawn=lambda: bridge)
    ws = FakeWS()
    await session.attach(ws)

    closed = await reg.close_key("old", websocket_code=4409)

    assert closed is True
    assert ws.close_code == 4409
    assert bridge.closed is True
    assert "old" not in reg._sessions


@pytest.mark.asyncio
async def test_replacement_retires_old_token_and_rejects_late_arrival():
    reg = make_registry()
    old_bridge = FakeBridge([b"", b""])
    old, _ = await reg.attach_or_spawn("old", spawn=lambda: old_bridge)
    old_ws = FakeWS()
    await old.attach(old_ws)

    new, created = await reg.attach_or_spawn(
        "new",
        replace_keys=["old"],
        replaced_websocket_code=4409,
        spawn=lambda: FakeBridge([b"", b""]),
    )

    assert created is True
    assert new.key == "new"
    assert old_bridge.closed is True
    assert old_ws.close_code == 4409
    with pytest.raises(RetiredToken):
        await reg.attach_or_spawn("old", spawn=lambda: FakeBridge([]))
    with pytest.raises(RetiredToken):
        await reg.attach("old", old, FakeWS())
    await reg.close_all()


@pytest.mark.asyncio
async def test_attach_replay_failure_leaves_session_detached():
    reg = make_registry()
    session, _ = await reg.attach_or_spawn(
        "tok", spawn=lambda: FakeBridge([b"buffered", b""])
    )
    await asyncio.sleep(0.02)

    class FailingWS(FakeWS):
        async def send_bytes(self, data):
            raise RuntimeError("socket closed during replay")

    ws = FailingWS()
    with pytest.raises(RuntimeError, match="socket closed"):
        await reg.attach("tok", session, ws)

    assert session.attached is False
    assert session._ws is None
    assert session.last_detached_at is not None
    await reg.close_all()


@pytest.mark.asyncio
async def test_live_send_failure_detaches_and_closes_transport():
    release_chunk = threading.Event()

    class BlockingBridge(FakeBridge):
        def __init__(self):
            super().__init__([])
            self._sent = False

        def read(self, timeout):
            if not self._sent:
                release_chunk.wait(timeout=2)
                self._sent = True
                return b"live output"
            return b""

    class FailingWS(FakeWS):
        async def send_bytes(self, data):
            raise RuntimeError("transport lost")

    session = PtySession("tok", BlockingBridge(), buffer_cap=1024, read_timeout=0.01)
    await session.start()
    ws = FailingWS()
    await session.attach(ws)
    release_chunk.set()
    await asyncio.sleep(0.05)

    assert session.attached is False
    assert session._ws is None
    assert session.last_detached_at is not None
    assert ws.close_code == 1001
    await session.close()


@pytest.mark.asyncio
async def test_replacement_waits_for_inflight_old_spawn_then_retires_it():
    reg = make_registry()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    old_bridge = FakeBridge([b"", b""])

    def spawn_old():
        spawn_started.set()
        release_spawn.wait(timeout=2)
        return old_bridge

    old_task = asyncio.create_task(reg.attach_or_spawn("old", spawn=spawn_old))
    assert await asyncio.to_thread(spawn_started.wait, 1)
    new_task = asyncio.create_task(
        reg.attach_or_spawn(
            "new",
            replace_keys=["old"],
            replaced_websocket_code=4409,
            spawn=lambda: FakeBridge([b"", b""]),
        )
    )
    await asyncio.sleep(0.02)
    release_spawn.set()

    await old_task
    new, _ = await new_task

    assert new.key == "new"
    assert old_bridge.closed is True
    assert set(reg._sessions) == {"new"}
    await reg.close_all()


@pytest.mark.asyncio
async def test_capacity_eviction_finishes_teardown_before_new_spawn():
    reg = make_registry(max_sessions=1)
    close_started = threading.Event()
    release_close = threading.Event()
    new_spawned = threading.Event()

    class SlowCloseBridge(FakeBridge):
        def close(self):
            close_started.set()
            release_close.wait(timeout=2)
            super().close()

    old_bridge = SlowCloseBridge([b"", b""])
    old, _ = await reg.attach_or_spawn("old", spawn=lambda: old_bridge)
    old_ws = FakeWS()
    await old.attach(old_ws)
    old.detach(old_ws)

    def spawn_new():
        new_spawned.set()
        return FakeBridge([b"", b""])

    task = asyncio.create_task(reg.attach_or_spawn("new", spawn=spawn_new))
    assert await asyncio.to_thread(close_started.wait, 1)
    assert new_spawned.is_set() is False
    release_close.set()
    await task

    assert old_bridge.closed is True
    assert new_spawned.is_set() is True
    await reg.close_all()


@pytest.mark.asyncio
async def test_reap_idle_closes_sessions_past_ttl():
    reg = make_registry(ttl=10.0)
    b = FakeBridge([b"", b""])
    s, _ = await reg.attach_or_spawn("tok", spawn=lambda: b)
    ws = FakeWS()
    await s.attach(ws)
    s.detach(ws)
    s.last_detached_at = time.monotonic() - 11.0   # detached 11s ago, ttl 10s
    await reg.reap_idle()
    assert b.closed is True
    s2, created = await reg.attach_or_spawn("tok", spawn=lambda: FakeBridge([]))
    assert created is True
    await reg.close_all()


@pytest.mark.asyncio
async def test_new_key_at_capacity_raises_when_none_reapable():
    reg = make_registry(max_sessions=1)
    b = FakeBridge([b"", b""])
    s, _ = await reg.attach_or_spawn("a", spawn=lambda: b)
    await s.attach(FakeWS())                    # attached → not reapable
    with pytest.raises(RegistryFull):
        await reg.attach_or_spawn("b", spawn=lambda: FakeBridge([]))
    await reg.close_all()


def test_reaper_interval_tracks_short_ttl_without_polling_faster_than_once_per_second():
    assert make_registry(ttl=0.1).reaper_interval == 1.0
    assert make_registry(ttl=5.0).reaper_interval == 5.0
    assert make_registry(ttl=300.0).reaper_interval == 60.0


@pytest.mark.asyncio
async def test_reaper_loop_invokes_reap(monkeypatch):
    from hermes_cli.pty_session import run_reaper
    reg = make_registry()
    calls = {"n": 0}

    async def fake_reap(now=None):
        calls["n"] += 1

    monkeypatch.setattr(reg, "reap_idle", fake_reap)
    task = asyncio.create_task(run_reaper(reg, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2
