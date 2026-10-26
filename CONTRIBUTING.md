# Contributing

* Python 3.10+, standard library only at runtime (Tkinter, sqlite3, urllib). Dev tools: `requirements-dev.txt`.
* Run tests: `python -m pytest -q` (no credentials or network needed; HTTP is mocked).
* Build the exe: `./build.ps1` (Windows).
* New data sources: subclass `RecentPlayersProvider` in `brpw/providers.py` and add tests with mocked responses.
  Never invent response formats; link a primary source in `docs/RESEARCH.md`.
* Schema changes: append a function to `MIGRATIONS` in `brpw/db.py`. Never edit an existing migration.
* Never commit tokens, `auth.json`, `.db` files or personal data.
