"""Initialize Home Assistant before collecting integration tests.

Match the live host startup order and keep combined runtime-test shutdown clean.
"""

import homeassistant  # noqa: F401
