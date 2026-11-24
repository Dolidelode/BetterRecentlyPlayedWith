import gc
import time

import pytest


@pytest.fixture(autouse=True)
def _collect_tk_garbage_on_the_main_thread():
    """Closed windows leave Tk variables in reference cycles. Python's automatic collection may run inside any
    thread, and in a worker thread (with no mainloop running in tests) Tk refuses the call and that worker dies.
    So during tests automatic collection is off and it runs after every test, on the main thread. The real app
    runs a mainloop, which handles such calls."""
    was = gc.isenabled()
    gc.disable()
    yield
    gc.collect()
    if was:
        gc.enable()


@pytest.fixture(autouse=True)
def _no_background_profile_requests(monkeypatch):
    """The app fills in profile pictures and hours by itself; in tests that must never reach Xbox."""
    from brpw import ui
    monkeypatch.setattr(ui.App, "fetch_profiles", staticmethod(lambda key, xuids: {}))
    monkeypatch.setattr(ui.App, "fetch_minutes", staticmethod(lambda key, xuids: {}))
    monkeypatch.setattr(ui.App, "fetch_account_picture", staticmethod(lambda key: ""))
    monkeypatch.setattr(ui.App, "fetch_crew", staticmethod(lambda key: None))
    monkeypatch.setattr(ui.App, "fetch_discord", staticmethod(lambda cfg, account, players, force: "unchanged"))


def new_root():
    """A Tk window for a test. On Windows a second Tk in one process sometimes fails to find its tk.tcl right after
    the first one closed, so try a few times before giving up (skip only when there really is no display)."""
    import tkinter as tk
    for attempt in range(8):
        try:
            return tk.Tk()
        except tk.TclError as e:
            if attempt == 7:
                pytest.skip(f"no display available: {e}")
            time.sleep(0.25)
