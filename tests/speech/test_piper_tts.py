"""Tests for the Piper TTS backend (piper itself is faked)."""

from __future__ import annotations

import io
import sys
import types
import wave

import pytest

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech import piper_tts
from openjarvis.speech._tts_discovery import TTS_BACKEND_ORDER, default_voice_for


class _FakeVoice:
    loads: list = []

    def __init__(self, model, config_path):
        self.model = model
        self.config_path = config_path
        self.config = types.SimpleNamespace(sample_rate=22050)
        self.calls = []

    @classmethod
    def load(cls, model, config_path=None):
        voice = cls(model, config_path)
        cls.loads.append(voice)
        return voice

    def synthesize_wav(self, text, wav_file, syn_config=None):
        self.calls.append((text, syn_config))
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(self.config.sample_rate)
        wav_file.writeframes(b"\x00\x00" * 2205)  # 0.1 s


class _FakeSynthesisConfig:
    def __init__(self, length_scale=None):
        self.length_scale = length_scale


@pytest.fixture
def fake_piper(monkeypatch, tmp_path):
    module = types.ModuleType("piper")
    module.PiperVoice = _FakeVoice
    module.SynthesisConfig = _FakeSynthesisConfig
    monkeypatch.setitem(sys.modules, "piper", module)
    monkeypatch.setenv("OPENJARVIS_PIPER_DIR", str(tmp_path))
    _FakeVoice.loads = []
    for name in ("ru_RU-dmitri-medium.onnx", "ru_RU-dmitri-medium.onnx.json"):
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def test_registered_and_ordered():
    # conftest clears registries between tests; re-register like the other backends.
    if not TTSRegistry.contains("piper"):
        TTSRegistry.register_value("piper", piper_tts.PiperTTSBackend)
    assert TTSRegistry.get("piper") is piper_tts.PiperTTSBackend
    assert "piper" in TTS_BACKEND_ORDER
    assert default_voice_for("piper") == "ru_RU-dmitri-medium"


def test_voice_url_layout():
    assert piper_tts.voice_url("ru_RU-dmitri-medium", ".onnx") == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx?download=true"
    )
    with pytest.raises(ValueError):
        piper_tts.voice_url("dmitri", ".onnx")


def test_synthesize_returns_wav(fake_piper):
    backend = piper_tts.PiperTTSBackend(download=False)
    result = backend.synthesize(
        "Привет, сэр", voice_id="ru_RU-dmitri-medium", speed=2.0
    )

    assert result.format == "wav"
    assert result.sample_rate == 22050
    assert result.duration_seconds == pytest.approx(0.1)
    assert result.metadata["backend"] == "piper"
    with wave.open(io.BytesIO(result.audio)) as reader:
        assert reader.getnframes() == 2205
    text, syn = _FakeVoice.loads[0].calls[0]
    assert text == "Привет, сэр"
    assert syn.length_scale == pytest.approx(0.5)
    assert _FakeVoice.loads[0].model == str(fake_piper / "ru_RU-dmitri-medium.onnx")


def test_voice_is_cached(fake_piper):
    backend = piper_tts.PiperTTSBackend(download=False)
    backend.synthesize("раз")
    backend.synthesize("два", voice_id="")
    assert len(_FakeVoice.loads) == 1


def test_empty_text_gives_empty_audio(fake_piper):
    result = piper_tts.PiperTTSBackend(download=False).synthesize("   ")
    assert result.audio == b""
    assert result.duration_seconds == 0.0


def test_missing_voice_without_download(fake_piper):
    backend = piper_tts.PiperTTSBackend(download=False)
    with pytest.raises(RuntimeError, match="missing"):
        backend.synthesize("тест", voice_id="ru_RU-irina-medium")


def test_ensure_voice_downloads_missing_files(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENJARVIS_PIPER_DIR", str(tmp_path))
    fetched = []

    def fake_download(url, target):
        fetched.append(url)
        target.write_bytes(b"data")

    monkeypatch.setattr(piper_tts, "_download", fake_download)
    model, config = piper_tts.ensure_voice("ru_RU-irina-medium")
    assert model.read_bytes() == b"data" and config.read_bytes() == b"data"
    assert len(fetched) == 2
    piper_tts.ensure_voice("ru_RU-irina-medium")
    assert len(fetched) == 2  # cached: no second download


def test_health_reflects_install(monkeypatch, fake_piper):
    assert piper_tts.PiperTTSBackend().health() is True
    monkeypatch.setitem(sys.modules, "piper", None)
    assert piper_tts.PiperTTSBackend().health() is False


def test_russian_voices_listed():
    voices = piper_tts.PiperTTSBackend().available_voices()
    assert voices[0] == "ru_RU-dmitri-medium"
    assert all("-" in v for v in voices)
