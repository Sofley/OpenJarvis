"""Piper TTS backend — fast, fully offline neural voices (incl. Russian).

Requires the ``piper-tts`` package (``pip install piper-tts``). It ships
prebuilt wheels for Windows, macOS and Linux and runs on CPU through
onnxruntime, so it works on modest hardware without a GPU.

Voices are ONNX files from the ``rhasspy/piper-voices`` collection. A voice
ID is the model name, e.g. ``ru_RU-dmitri-medium``. Missing voices are
downloaded once into ``<OpenJarvis root>/models/piper`` (or the directory
given by ``OPENJARVIS_PIPER_DIR``); after that no network access is needed.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

_DEFAULT_VOICE_ID = "ru_RU-dmitri-medium"
_VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Curated voices: Russian first (the reason this backend exists), then a few
# English ones so a non-Russian voice is always one setting away.
_VOICES: Tuple[str, ...] = (
    "ru_RU-dmitri-medium",
    "ru_RU-denis-medium",
    "ru_RU-ruslan-medium",
    "ru_RU-irina-medium",
    "en_GB-alan-medium",
    "en_US-ryan-medium",
    "en_US-lessac-medium",
)


def voices_dir() -> Path:
    """Directory where Piper voice files are stored."""
    override = os.environ.get("OPENJARVIS_PIPER_DIR", "").strip()
    if override:
        return Path(override)
    from openjarvis.core.paths import get_data_dir

    return get_data_dir() / "models" / "piper"


def voice_url(voice_id: str, suffix: str) -> str:
    """Download URL of ``<voice_id><suffix>`` in rhasspy/piper-voices.

    ``ru_RU-dmitri-medium`` lives at ``ru/ru_RU/dmitri/medium/``.
    """
    try:
        lang_region, name, quality = voice_id.split("-", 2)
        family = lang_region.split("_", 1)[0]
    except ValueError as exc:
        raise ValueError(
            f"Invalid Piper voice ID {voice_id!r}; expected e.g. 'ru_RU-dmitri-medium'"
        ) from exc
    return (
        f"{_VOICES_URL}/{family}/{lang_region}/{name}/{quality}/"
        f"{voice_id}{suffix}?download=true"
    )


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    logger.info("Downloading Piper voice file %s", target.name)
    with urllib.request.urlopen(url, timeout=60) as resp, open(partial, "wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    partial.replace(target)


def ensure_voice(voice_id: str, *, download: bool = True) -> Tuple[Path, Path]:
    """Return ``(model.onnx, model.onnx.json)`` paths, downloading if missing."""
    base = voices_dir()
    model = base / f"{voice_id}.onnx"
    config = base / f"{voice_id}.onnx.json"
    for path, suffix in ((model, ".onnx"), (config, ".onnx.json")):
        if path.is_file() and path.stat().st_size > 0:
            continue
        if not download:
            raise RuntimeError(f"Piper voice file missing: {path}")
        _download(voice_url(voice_id, suffix), path)
    return model, config


def _import_piper() -> Any:
    try:
        import piper
    except ImportError as exc:
        raise RuntimeError(
            "piper-tts package not installed. Install with: pip install piper-tts"
        ) from exc
    return piper


@TTSRegistry.register("piper")
class PiperTTSBackend(TTSBackend):
    """Piper TTS — local, offline, CPU-friendly; good Russian voices."""

    backend_id = "piper"

    def __init__(self, *, download: bool = True, max_cached_voices: int = 2) -> None:
        if max_cached_voices < 1:
            raise ValueError("max_cached_voices must be at least 1")
        self._download = download
        self._max_cached = max_cached_voices
        self._voices: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def _load_voice(self, voice_id: str) -> Any:
        with self._lock:
            voice = self._voices.get(voice_id)
            if voice is not None:
                return voice
            piper = _import_piper()
            model, config = ensure_voice(voice_id, download=self._download)
            voice = piper.PiperVoice.load(str(model), config_path=str(config))
            if len(self._voices) >= self._max_cached:
                self._voices.pop(next(iter(self._voices)))
            self._voices[voice_id] = voice
            return voice

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = _DEFAULT_VOICE_ID,
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        voice_id = voice_id or _DEFAULT_VOICE_ID
        piper = _import_piper()
        speed = speed if speed and speed > 0 else 1.0
        # Piper's length_scale is the inverse of speed (2.0 = twice as slow).
        syn_config = piper.SynthesisConfig(length_scale=1.0 / speed)

        buf = io.BytesIO()
        with self._lock:
            voice = self._load_voice(voice_id)
            with wave.open(buf, "wb") as wav_file:
                if text.strip():
                    voice.synthesize_wav(text, wav_file, syn_config=syn_config)
                else:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(_sample_rate(voice))

        audio = buf.getvalue()
        buf.seek(0)
        with wave.open(buf, "rb") as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()

        if output_format.lower() != "wav":
            audio = _convert(audio, output_format)

        return TTSResult(
            audio=audio if frames else b"",
            format=output_format,
            voice_id=voice_id,
            sample_rate=rate,
            duration_seconds=frames / rate if rate else 0.0,
            metadata={"backend": "piper"},
        )

    def available_voices(self) -> List[str]:
        return list(_VOICES)

    def health(self) -> bool:
        try:
            _import_piper()
        except RuntimeError:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            self._voices.clear()


def _sample_rate(voice: Any) -> int:
    config = getattr(voice, "config", None)
    return int(getattr(config, "sample_rate", 22050) or 22050)


def _convert(wav_bytes: bytes, output_format: str) -> bytes:
    """Re-encode WAV to another soundfile-supported container (e.g. flac/ogg)."""
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(wav_bytes))
    out = io.BytesIO()
    sf.write(out, data, rate, format=output_format.upper())
    return out.getvalue()


__all__ = ["PiperTTSBackend", "ensure_voice", "voice_url", "voices_dir"]
