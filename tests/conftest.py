"""Bootstrap Home Assistant before schema imports, matching the live host.

Recent Home Assistant installs its schema compatibility module during startup;
loading voluptuous first can leave tests holding incompatible marker classes.
"""
import homeassistant  # noqa: F401
