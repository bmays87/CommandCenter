"""OpenWakeWord engine wrapper: model selection, scoring, API tolerance.

The real openwakeword library is stubbed via sys.modules so no model files
are needed; the wrapper's own logic is what's under test.
"""

import sys
import types
from typing import Any, ClassVar

import numpy
import pytest

from prodeo_wakeword_openwakeword import (
    OpenWakeWordConfig,
    OpenWakeWordDetector,
    _slug,
    manifest,
)


class FakeModel:
    """Stands in for openwakeword.model.Model."""

    instances: ClassVar[list["FakeModel"]] = []
    reject_new_kwarg = False

    def __init__(self, **kwargs: Any) -> None:
        if self.reject_new_kwarg and "wakeword_models" in kwargs:
            raise TypeError("unexpected keyword argument 'wakeword_models'")
        self.kwargs = kwargs
        self.frames: list[Any] = []
        self.resets = 0
        FakeModel.instances.append(self)

    def predict(self, frame: Any) -> dict[str, float]:
        self.frames.append(frame)
        return {"model-a": 0.2, "model-b": 0.7}

    def reset(self) -> None:
        self.resets += 1


@pytest.fixture(autouse=True)
def fake_openwakeword(monkeypatch: pytest.MonkeyPatch) -> type[FakeModel]:
    FakeModel.instances = []
    FakeModel.reject_new_kwarg = False
    module = types.ModuleType("openwakeword.model")
    module.Model = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword.model", module)
    monkeypatch.setitem(sys.modules, "openwakeword", types.ModuleType("openwakeword"))
    return FakeModel


def test_slug() -> None:
    assert _slug("mjölnir") == "mjolnir"
    assert _slug("Hey Jarvis!") == "hey_jarvis"


def test_default_wake_word_falls_back_to_stock_model() -> None:
    detector = OpenWakeWordDetector(OpenWakeWordConfig())
    model = FakeModel.instances[0]
    # no custom "mjölnir" model ships yet: the stock fallback is loaded
    assert model.kwargs["wakeword_models"] == ["hey_jarvis"]
    assert model.kwargs["inference_framework"] == "onnx"
    assert detector.name == "openwakeword"


def test_explicit_model_path_wins() -> None:
    OpenWakeWordDetector(OpenWakeWordConfig(model_path="/opt/models/custom.onnx"))
    assert FakeModel.instances[0].kwargs["wakeword_models"] == ["/opt/models/custom.onnx"]


def test_old_openwakeword_argument_name_is_tolerated() -> None:
    FakeModel.reject_new_kwarg = True
    OpenWakeWordDetector(OpenWakeWordConfig(fallback_model="hey_jarvis"))
    assert FakeModel.instances[-1].kwargs == {"wakeword_model_paths": ["hey_jarvis"]}


def test_process_scores_max_and_reset() -> None:
    detector = OpenWakeWordDetector(OpenWakeWordConfig())
    frame = numpy.zeros(1280, dtype=numpy.int16).tobytes()
    assert detector.process(frame) == 0.7
    sent = FakeModel.instances[0].frames[0]
    assert sent.dtype == numpy.int16 and len(sent) == 1280
    detector.reset()
    assert FakeModel.instances[0].resets == 1


def test_manifest_shape() -> None:
    m = manifest()
    assert (m.name, m.kind) == ("openwakeword", "wakeword")
    assert m.config_model is OpenWakeWordConfig
