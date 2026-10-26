"""Background synchronisation: one job at a time, never blocks the UI, never fakes success."""
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .db import Database, MergeResult
from .providers import ProviderError, RecentPlayersProvider


@dataclass
class SyncOutcome:
    ok: bool
    message: str
    result: Optional[MergeResult] = None
    reauth: bool = False
    finished_at: int = 0


class SyncService:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()

    def run(self, provider: RecentPlayersProvider) -> Optional[SyncOutcome]:
        """Synchronous sync; returns None if another sync is already running."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            now = int(time.time())
            try:
                players = provider.fetch()
                res = self.db.merge_players(players, provider.name, provider.live, now)
            except ProviderError as e:
                return SyncOutcome(False, str(e), reauth=e.reauth, finished_at=now)
            except Exception as e:  # keep data untouched, report honestly
                return SyncOutcome(False, f"Unexpected error: {e}", finished_at=now)
            if provider.live:
                self.db.set_setting("last_sync", str(now))
            hook, friends_note = getattr(provider, "post_sync", None), ""
            if hook:
                try:
                    hook(self.db)
                except Exception as e:  # secondary data: report, never fail the sync
                    friends_note = f" (friends list not updated: {e})"
            note = f" ({getattr(provider, 'notes', '')})" if getattr(provider, "notes", "") else ""
            return SyncOutcome(True, f"{res.new} new, {res.known} already known{note}{friends_note}", res,
                               finished_at=now)
        finally:
            self._lock.release()

    def run_async(self, provider: RecentPlayersProvider, done: Callable[[Optional[SyncOutcome]], None]) -> None:
        threading.Thread(target=lambda: done(self.run(provider)), daemon=True).start()
