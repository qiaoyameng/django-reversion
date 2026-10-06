from test_project.settings import *  # noqa

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
    "postgres": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
    "mysql": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}
