from .settings import *  # noqa

# Local test shim: the CI environment provides real Postgres/MySQL servers,
# but a developer checkout may not have the drivers installed. Point the extra
# aliases at in-memory SQLite databases so the multi-db test suite can run.
DATABASES["postgres"] = {  # noqa: F405
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": ":memory:",
}
DATABASES["mysql"] = {  # noqa: F405
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": ":memory:",
}
