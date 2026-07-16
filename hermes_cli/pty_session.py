"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task
always reads the PTY into a bounded RingBuffer and forwards to the attached
socket when present. Reconnecting with the same opaque token replays the
buffer and resumes live. See
docs/superpowers/specs/2026-06-20-pty-keepalive-reattach-design.md.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Optional

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409

DEFAULT_PTY_KEEPALIVE_TTL_SECONDS = 30 * 60.0
DEFAULT_PTY_MAX_SESSIONS = 16


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, float) and not value.is_integer():
            return default
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_registry_settings(config: Any) -> tuple[float, int]:
    """Resolve dashboard PTY keep-alive limits from config.yaml.

    Invalid values preserve the existing bounded defaults instead of disabling
    either guardrail. The settings live under ``dashboard`` because each PTY is
    a full dashboard-owned TUI process with its own MCP children.
    """
    dashboard = config.get("dashboard") if isinstance(config, dict) else None
    if not isinstance(dashboard, dict):
        dashboard = {}
    ttl = _positive_float(
        dashboard.get("pty_keepalive_ttl_seconds"),
        DEFAULT_PTY_KEEPALIVE_TTL_SECONDS,
    )
    max_sessions = _positive_int(
        dashboard.get("pty_max_sessions"),
        DEFAULT_PTY_MAX_SESSIONS,
    )
    return ttl, max_sessions


class RingBuffer:
    """Keeps only the most recent ``capacity`` bytes appended to it."""

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._buf = bytearray()
        self._truncated = False

    def append(self, data: bytes) -> None:
        self._buf.extend(data)
        overflow = len(self._buf) - self._cap
        if overflow > 0:
            del self._buf[:overflow]
            self._truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._buf)

    @property
    def truncated(self) -> bool:
        return self._truncated


class PtySession:
    def __init__(self, key: str, bridge, *, buffer_cap: int, read_timeout: float) -> None:
        self.key = key
        self.bridge = bridge
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.attached = False
        self.last_detached_at: Optional[float] = None
        self._read_timeout = read_timeout
        self._ws = None
        self._drain_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self.bridge.read, self._read_timeout)
            if chunk is None:                       # EOF — the agent process exited
                self.alive = False
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.close(code=WS_CLOSE_PROCESS_EXITED)
                    except Exception:
                        pass
                return
            if not chunk:                            # idle tick
                await asyncio.sleep(0)
                continue
            self.buffer.append(chunk)
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    # A failed live send can leave the endpoint blocked in
                    # receive() on a half-open transport. Mark it detached so
                    # TTL/capacity cleanup can reclaim the PTY, and actively
                    # close with 1001 so the browser reconnects to this token.
                    if self._ws is ws:
                        self.detach(ws)
                        try:
                            await ws.close(code=1001)
                        except Exception:
                            pass

    async def attach(self, ws) -> None:
        old = self._ws
        if old is not None and old is not ws:
            try:
                await old.close(code=WS_CLOSE_SUPERSEDED)
            except Exception:
                pass
        self._ws = ws
        self.attached = True
        self.last_detached_at = None
        try:
            snap = self.buffer.snapshot()
            if snap:
                await ws.send_bytes(snap)
        except Exception:
            # Replay occurs before the endpoint's writer-loop finally is
            # installed. Undo attachment here so the registry can reap this
            # process tree after a socket dies during replay.
            self.detach(ws)
            raise

    def detach(self, ws) -> None:
        # Only the currently-attached socket may mark the session detached.
        # A superseded socket's handler also calls detach on its way out
        # (its ``finally`` runs after the new tab attached); flipping
        # ``attached`` then would make a session with a live viewer look
        # idle and reapable.
        if self._ws is not ws:
            return
        self._ws = None
        self.attached = False
        self.last_detached_at = time.monotonic()

    async def close(self, *, websocket_code: Optional[int] = None) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        self.alive = False
        ws = self._ws
        self._ws = None
        self.attached = False
        if ws is not None and websocket_code is not None:
            try:
                await ws.close(code=websocket_code)
            except Exception:
                pass
        try:
            # bridge.close() joins the child — blocking; keep it off the
            # event loop (#53227).
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            pass


from typing import Callable, Dict, Iterable, Tuple


class RegistryFull(Exception):
    pass


class RetiredToken(Exception):
    """An attach token explicitly superseded by a newer dashboard chat."""


async def run_reaper(
    registry: "PtySessionRegistry", *, interval: Optional[float] = None
) -> None:
    """Periodically reap idle/dead keep-alive sessions. Cancelled on shutdown."""
    poll_interval = registry.reaper_interval if interval is None else interval
    while True:
        await asyncio.sleep(poll_interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    _MAX_RETIRED_TOKENS = 1024

    def __init__(self, *, ttl: float, max_sessions: int,
                 buffer_cap: int, read_timeout: float) -> None:
        self._ttl = ttl
        self._max = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._sessions: Dict[str, PtySession] = {}
        self._retired_tokens: Dict[str, None] = {}
        # Spawn is offloaded and awaited, so concurrent WebSocket connects can
        # otherwise all pass the capacity check before any session is inserted.
        # All registry mutations share this lock so replacement, teardown, and
        # capacity accounting remain atomic around the awaited process work.
        self._attach_lock = asyncio.Lock()

    @property
    def reaper_interval(self) -> float:
        # Honor short keep-alive TTLs promptly without turning a pathological
        # sub-second value into a busy polling loop.
        return max(1.0, min(60.0, self._ttl))

    def _remember_retired(self, key: str) -> None:
        self._retired_tokens.pop(key, None)
        self._retired_tokens[key] = None
        while len(self._retired_tokens) > self._MAX_RETIRED_TOKENS:
            self._retired_tokens.pop(next(iter(self._retired_tokens)))

    async def attach_or_spawn(
        self,
        key: str,
        *,
        spawn: Callable[[], object],
        replace_keys: Iterable[str] = (),
        replaced_websocket_code: Optional[int] = None,
    ) -> Tuple[PtySession, bool]:
        async with self._attach_lock:
            # Retire replacements while holding the same lock as spawn. A late
            # request for an old token is then rejected even if its first spawn
            # was still in flight when the fresh request arrived.
            for old_key in dict.fromkeys(replace_keys):
                if not old_key or old_key == key:
                    continue
                self._remember_retired(old_key)
                old_session = self._sessions.pop(old_key, None)
                if old_session is not None:
                    await old_session.close(websocket_code=replaced_websocket_code)

            await self._reap_idle_locked()
            if key in self._retired_tokens:
                raise RetiredToken(key)

            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return existing, False
            if existing is not None:                       # dead remnant
                await existing.close()
                self._sessions.pop(key, None)
            if len(self._sessions) >= self._max:
                await self._reap_one_idle_or_raise_locked()
            # PTY spawn does blocking fork/exec work — keep it off the event
            # loop (#53227). The lock stays held until insertion so another
            # connect cannot overbook the registry while spawn is in flight.
            bridge = await asyncio.to_thread(spawn)
            session = PtySession(key, bridge, buffer_cap=self._buffer_cap,
                                 read_timeout=self._read_timeout)
            await session.start()
            self._sessions[key] = session
            return session, True

    async def attach(self, key: str, session: PtySession, ws) -> None:
        async with self._attach_lock:
            current = self._sessions.get(key)
            if key in self._retired_tokens or current is not session or not session.alive:
                raise RetiredToken(key)
            await session.attach(ws)

    def detach(self, key: str, ws) -> None:
        s = self._sessions.get(key)
        if s is not None:
            s.detach(ws)

    async def close_key(
        self,
        key: str,
        *,
        websocket_code: Optional[int] = None,
        retire: bool = False,
    ) -> bool:
        async with self._attach_lock:
            if retire:
                self._remember_retired(key)
            session = self._sessions.pop(key, None)
            if session is None:
                return False
            await session.close(websocket_code=websocket_code)
            return True

    async def _reap_idle_locked(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        doomed = [
            key for key, s in self._sessions.items()
            if (not s.alive)
            or (not s.attached and s.last_detached_at is not None
                and (now - s.last_detached_at) > self._ttl)
        ]
        for key in doomed:
            await self._sessions.pop(key).close()

    async def reap_idle(self, now: Optional[float] = None) -> None:
        async with self._attach_lock:
            await self._reap_idle_locked(now)

    async def _reap_one_idle_or_raise_locked(self) -> None:
        idle = [s for s in self._sessions.values()
                if not s.attached and s.last_detached_at is not None]
        if not idle:
            raise RegistryFull(
                f"dashboard PTY limit reached ({len(self._sessions)}/{self._max}); "
                "close an attached chat and try again"
            )
        oldest = min(idle, key=lambda s: s.last_detached_at or 0.0)
        self._sessions.pop(oldest.key, None)
        # Finish process-group teardown before spawning the replacement. The
        # configured maximum is a resource bound, not merely a dict-size bound.
        await oldest.close()

    async def close_all(self) -> None:
        async with self._attach_lock:
            for key in list(self._sessions):
                await self._sessions.pop(key).close()
