"""Requests waiting on a human (a human seat's move or a human judge's ruling)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_ids = itertools.count(1)


@dataclass
class PendingRequest:
    id: str
    kind: str  # propose | discuss | vote | ruling
    seat: str  # player name, or "judge"
    context: dict[str, Any]
    future: asyncio.Future[dict[str, Any]] = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "seat": self.seat, "context": self.context}


class HumanInbox:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.pending: dict[str, PendingRequest] = {}
        self.on_change = on_change

    def list(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.pending.values()]

    async def request(
        self, kind: str, seat: str, context: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any] | None:
        """Wait for a human answer. Returns None on timeout."""
        req = PendingRequest(
            id=str(next(_ids)),
            kind=kind,
            seat=seat,
            context=context,
            future=asyncio.get_running_loop().create_future(),
        )
        self.pending[req.id] = req
        self._changed()
        try:
            return await asyncio.wait_for(asyncio.shield(req.future), timeout)
        except TimeoutError:
            return None
        finally:
            self.pending.pop(req.id, None)
            self._changed()

    def respond(self, req_id: str, response: dict[str, Any]) -> None:
        req = self.pending.get(req_id)
        if req is None:
            raise KeyError(req_id)
        if not req.future.done():
            req.future.set_result(response)

    def cancel_all(self) -> None:
        for req in list(self.pending.values()):
            if not req.future.done():
                req.future.cancel()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()
