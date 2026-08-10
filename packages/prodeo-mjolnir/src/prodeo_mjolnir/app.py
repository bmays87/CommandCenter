"""What Mjölnir tells the server about itself as an app extension.

Mjölnir is a client, not a plugin: it runs in its own process and talks over
the same API as the dashboard. The ``prodeo.apps`` entry point is how the
server discovers it, renders a setup form, and supervises it (ADR-0014).

Everything the supervisor needs is declarative. ``env_prefix`` is what keeps
core generic: Mjölnir reads its settings through pydantic-settings, so the
server can derive the whole child environment from the saved config without
knowing a single field name here.
"""

from prodeo.apps import AppManifest
from prodeo_mjolnir.config import MjolnirSettings

VERSION = "0.1.0"

#: The handful of settings that actually need a human decision. The other ~30
#: are tuning knobs with working defaults and belong behind "Advanced" - a
#: setup wizard that opens with 33 fields is not a setup wizard.
SETUP_FIELDS = [
    "engines",
    "wake_word",
    "honorific",
    "persona_pack",
    "stt_plugin",
    "tts_plugin",
    "wakeword_plugin",
    "vad_threshold",
]


def manifest() -> AppManifest:
    """Entry point (``prodeo.apps`` group): what this app is."""
    return AppManifest(
        name="mjolnir",
        version=VERSION,
        config_model=MjolnirSettings,
        command=["prodeo-mjolnir"],
        env_prefix="MJOLNIR_",
        setup_fields=SETUP_FIELDS,
        presence_client_id="mjolnir",
        # The server fills these in so a locally supervised client is never
        # asked to be told its own server's address or token.
        server_url_field="server_url",
        api_token_field="api_token",
        description=(
            "Offline voice client: wake word, speech-to-text, and a spoken "
            "assistant that answers questions and approves permissions by voice."
        ),
        publisher="Prodeo",
        homepage="https://github.com/bmays87/CommandCenter/tree/main/packages/prodeo-mjolnir",
        license="Apache-2.0",
        categories=["voice", "client"],
    )


__all__ = ["SETUP_FIELDS", "VERSION", "manifest"]
