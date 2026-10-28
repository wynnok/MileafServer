"""Bounded process-local JSON byte cache and private conditional responses."""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from time import monotonic

from fastapi import Request, Response


class TTLCache:
    """Used on the application's event loop; cached bytes preserve data freshness."""

    def __init__(
        self, *, ttl: int = 30, capacity: int = 256, clock: Callable[[], float] = monotonic
    ) -> None:
        if ttl < 0 or capacity < 1:
            raise ValueError("Cache TTL must be nonnegative and capacity must be positive")
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock
        self.hits = 0
        self.misses = 0
        self.entries: OrderedDict[Hashable, tuple[float, bytes]] = OrderedDict()

    def get(self, key: Hashable) -> bytes | None:
        entry = self.entries.get(key)
        if self.ttl > 0 and entry is not None and entry[0] > self.clock():
            self.entries.move_to_end(key)
            self.hits += 1
            return entry[1]
        self.entries.pop(key, None)
        self.misses += 1
        return None

    def put(self, key: Hashable, body: bytes, *, ttl: int | None = None) -> None:
        lifetime = self.ttl if ttl is None else ttl
        if self.ttl == 0 or lifetime <= 0:
            return
        now = self.clock()
        for expired in [key for key, (expiry, _) in self.entries.items() if expiry <= now]:
            del self.entries[expired]
        self.entries[key] = (now + lifetime, body)
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self.entries)


def conditional_response(
    request: Request, body: bytes, *, ttl: int, validator_body: bytes | None = None
) -> Response:
    # Include every query parameter even when two requests currently serialize identically.
    identity = json.dumps([request.url.path, request.url.query], separators=(",", ":")).encode()
    content = body if validator_body is None else validator_body
    etag = '"' + hashlib.sha256(identity + b"\0" + content).hexdigest() + '"'
    if validator_body is not None:
        # Generation timestamps can change while the data representation is equivalent.
        etag = "W/" + etag
    headers = {"ETag": etag, "Cache-Control": f"private, max-age={ttl}"}
    candidates = [value.strip() for value in request.headers.get("if-none-match", "").split(",")]
    matches = any(
        value == "*" or value.removeprefix("W/") == etag.removeprefix("W/") for value in candidates
    )
    if request.method in {"GET", "HEAD"} and matches:
        return Response(status_code=304, headers=headers)
    return Response(content=body, headers=headers, media_type="application/json")


async def cached_response(
    request: Request,
    *,
    cache: TTLCache,
    key: Hashable,
    render: Callable[[], Awaitable[bytes]],
    ttl: int,
) -> Response:
    """Callers declare HTTP/server TTL and a key containing all representation inputs."""
    body = cache.get(key) if ttl > 0 else None
    if body is None:
        body = await render()
        if ttl > 0:
            cache.put(key, body, ttl=ttl)
    return conditional_response(request, body, ttl=ttl)
