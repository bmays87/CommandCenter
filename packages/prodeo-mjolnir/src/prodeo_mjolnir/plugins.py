"""Engine loading: the voice client's side of the shared plugin mechanism.

Engines are ordinary Prodeo plugins (``prodeo.plugins`` entry points with a
``PluginManifest``); this loader is the *host* for the voice kinds -
``wakeword``, ``stt``, ``tts`` - which the server's PluginHost deliberately
skips. ``summarizer``-kind plugins are also collected so the optional
persona rephraser can ride the same local-model path as the daily summary.

Unlike the server host, engine load failures here are fatal: a voice client
without its ears or voice has nothing to contain into.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol

import structlog

from prodeo.plugins import PLUGIN_API_VERSION, PLUGIN_ENTRY_POINT_GROUP, PluginManifest
from prodeo.summary.interface import Summarizer
from prodeo_mjolnir.config import MjolnirSettings
from prodeo_mjolnir.engines import SpeechToText, TextToSpeech, WakeWordDetector
from prodeo_mjolnir.errors import EngineNotFoundError, MjolnirError

_log = structlog.get_logger(__name__)

_ENGINE_PROTOCOLS: dict[str, type] = {
    "wakeword": WakeWordDetector,
    "stt": SpeechToText,
    "tts": TextToSpeech,
}


@dataclass
class Engines:
    """The three live engines the pipeline needs, plus optional rephrasers."""

    wakeword: WakeWordDetector
    stt: SpeechToText
    tts: TextToSpeech
    rephrasers: dict[str, Summarizer] = field(default_factory=dict)


class _EntryPointLike(Protocol):
    """The slice of ``importlib.metadata.EntryPoint`` the loader uses."""

    @property
    def name(self) -> str: ...

    def load(self) -> Any: ...


def _installed_entry_points() -> Iterable[_EntryPointLike]:
    return entry_points(group=PLUGIN_ENTRY_POINT_GROUP)


def _manifests(eps: Iterable[_EntryPointLike]) -> list[PluginManifest]:
    found: list[PluginManifest] = []
    for ep in eps:
        try:
            obj = ep.load()
            manifest = obj if isinstance(obj, PluginManifest) else obj()
        except Exception:
            _log.warning("engines.entry_point_unloadable", entry_point=ep.name)
            continue
        if isinstance(manifest, PluginManifest):
            found.append(manifest)
    return found


# Returns Any deliberately: the plugin product's type is only known by kind,
# and every caller isinstance-checks it against the expected Protocol.
def _instantiate(manifest: PluginManifest, config: dict[str, Any]) -> Any:
    if manifest.plugin_api_version != PLUGIN_API_VERSION:
        raise MjolnirError(
            f"plugin API version mismatch: {manifest.name!r} declares "
            f"{manifest.plugin_api_version}, this client provides {PLUGIN_API_VERSION}"
        )
    validated: Any = config
    if manifest.config_model is not None:
        validated = manifest.config_model.model_validate(config)
    return manifest.factory(validated)


def load_engines(
    settings: MjolnirSettings,
    *,
    entry_points_fn: Callable[[], Iterable[_EntryPointLike]] = _installed_entry_points,
) -> Engines:
    """Resolve and instantiate the configured engines.

    The top-level ``wake_word`` setting is merged into the wake word engine's
    config (as ``wake_word``) unless its per-engine config already sets one,
    so users configure the word once regardless of engine.
    """
    manifests = _manifests(entry_points_fn())
    by_kind: dict[str, dict[str, PluginManifest]] = {}
    for manifest in manifests:
        by_kind.setdefault(manifest.kind, {})[manifest.name] = manifest

    def pick(kind: str, name: str) -> PluginManifest:
        manifest = by_kind.get(kind, {}).get(name)
        if manifest is None:
            raise EngineNotFoundError(kind, name, list(by_kind.get(kind, {})))
        return manifest

    wake_config = dict(settings.engines.get(settings.wakeword_plugin, {}))
    wake_config.setdefault("wake_word", settings.wake_word)

    engines: dict[str, Any] = {}
    for kind, name, config in (
        ("wakeword", settings.wakeword_plugin, wake_config),
        ("stt", settings.stt_plugin, settings.engines.get(settings.stt_plugin, {})),
        ("tts", settings.tts_plugin, settings.engines.get(settings.tts_plugin, {})),
    ):
        engine = _instantiate(pick(kind, name), dict(config))
        protocol = _ENGINE_PROTOCOLS[kind]
        if not isinstance(engine, protocol):
            raise MjolnirError(f"{kind} plugin {name!r} did not produce a {protocol.__name__}")
        engines[kind] = engine
        _log.info("engines.loaded", kind=kind, plugin=name)

    rephrasers: dict[str, Summarizer] = {}
    for name, manifest in by_kind.get("summarizer", {}).items():
        try:
            product = _instantiate(manifest, dict(settings.engines.get(name, {})))
        except Exception:
            _log.warning("engines.rephraser_failed", plugin=name)
            continue
        if isinstance(product, Summarizer):
            rephrasers[name] = product

    return Engines(
        wakeword=engines["wakeword"],
        stt=engines["stt"],
        tts=engines["tts"],
        rephrasers=rephrasers,
    )
