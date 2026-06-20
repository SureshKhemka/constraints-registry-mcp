"""Bundle store (FR-VERSION-2/3).

Holds successive immutable bundles and lets a consumer fetch a specific bundle
by id or default to the latest. Bundles themselves are frozen (immutable); the
store never mutates a stored bundle. Adding a bundle whose id already exists is
idempotent, preserving immutability of an already-published version.
"""

from __future__ import annotations

from .bundle import Bundle


class BundleStore:
    def __init__(self) -> None:
        self._order: list[str] = []
        self._by_id: dict[str, Bundle] = {}
        # The currently-published bundle. Tracked separately from insertion order
        # so that re-publishing a previously-seen bundle (e.g. a reload that
        # reverts to earlier content, same hash) correctly moves "latest" back to
        # it rather than leaving it on a newer-but-stale version.
        self._current_id: str | None = None

    def add(self, bundle: Bundle) -> Bundle:
        if bundle.bundle_id not in self._by_id:
            self._by_id[bundle.bundle_id] = bundle
            self._order.append(bundle.bundle_id)
        self._current_id = bundle.bundle_id
        return self._by_id[bundle.bundle_id]

    def latest(self) -> Bundle | None:
        return self._by_id[self._current_id] if self._current_id else None

    def get(self, bundle_id: str | None = None) -> Bundle | None:
        """Fetch a pinned bundle by id, or the latest when ``bundle_id`` is None
        (FR-VERSION-3 default-to-latest)."""
        if bundle_id is None:
            return self.latest()
        return self._by_id.get(bundle_id)

    def versions(self) -> list[str]:
        return list(self._order)

    def __len__(self) -> int:
        return len(self._order)
