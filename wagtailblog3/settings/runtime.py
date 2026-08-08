"""Select the settings profile from the process environment."""

import os


_environment = os.environ.get("WAGTAILBLOG_ENV", "test").strip().lower()
if _environment == "production":
    from .production import *  # noqa: F401,F403
else:
    from .dev import *  # noqa: F401,F403
