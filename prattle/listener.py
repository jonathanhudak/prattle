"""Continuous microphone listener using parakeet-mlx.

Streams the mic into fixed-size windows, transcribes each with Parakeet, and
appends each non-empty utterance as a JSON line to `transcript.jsonl`.

Run with:
    python -m prattle listen

The model is loaded lazily on first chunk so failed imports don't crash the
whole CLI on systems where parakeet-mlx isn't available (e.g. Intel Mac).
"""
from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import Config
from .utils import append_jsonl, now_iso

log = logging.getLogger("listener")

# Set by the TUI to suspend transcript writing without stopping the mic stream.
_PAUSED: threading.Event = threading.Event()


class ParakeetTranscriber:
    """Thin wrapper around parakeet-mlx so the import is deferred."""

    def __init__(self, model_id: str) -> None:
        log.info("loading parakeet-mlx model %s ...", model_id)
        try:
            from parakeet_mlx import from_pretrained  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "parakeet-mlx is not installed. `pip install parakeet-mlx` "
                "(Apple Silicon only). For non-Mac, use `python -m prattle replay <file>`."
            ) from e
        self.model = from_pretrained(model_id)
        log.info("parakeet-mlx loaded.")

    def transcribe_audio(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe a 1D float32 audio array. Returns the recognized text."""
        # parakeet-mlx accepts paths or numpy arrays depending on version.
        # We try both. Prefer in-memory.
        try:
            result = self.model.transcribe(audio)  # type: ignore[arg-type]
        except Exception:
            # Fallback: write to a temp WAV and pass path
            import io
            import tempfile
            import wave

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                wav_path = tf.name
            try:
                int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(int16.tobytes())
                result = self.model.transcribe(wav_path)
            finally:
                try:
                    Path(wav_path).unlink()
                except OSError:
                    pass
        # Result objects vary. Handle the common shapes.
        if hasattr(result, "text"):
            return (result.text or "").strip()
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict) and "text" in result:
            return str(result["text"]).strip()
        return str(result).strip()


def _make_callback(
    audio_q: "queue.Queue[np.ndarray]",
    sample_rate: int,
    chunk_samples: int,
) -> tuple[Any, "list[np.ndarray]"]:
    """Build a sounddevice InputStream callback that collects samples into
    fixed-size chunks and pushes them on the queue.
    """
    buf: list[np.ndarray] = []
    accumulated = [0]  # mutable count

    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("sounddevice status: %s", status)
        # indata shape: (frames, channels). We requested mono.
        mono = indata[:, 0].copy()
        buf.append(mono)
        accumulated[0] += frames
        while accumulated[0] >= chunk_samples:
            joined = np.concatenate(buf)
            chunk = joined[:chunk_samples]
            remainder = joined[chunk_samples:]
            buf.clear()
            if remainder.size:
                buf.append(remainder)
            accumulated[0] = remainder.size
            audio_q.put(chunk)

    return callback, buf


def run_listener(
    config: Config,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Block until stop_event is set (or SIGINT if no event given), writing chunks to JSONL."""
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        log.error(
            "sounddevice not installed. `pip install sounddevice` (and on macOS, "
            "grant microphone permission to your terminal)."
        )
        return 2

    listener_cfg = config.get("listener", default={}) or {}
    sample_rate = int(listener_cfg.get("sample_rate", 16000))
    chunk_seconds = float(listener_cfg.get("chunk_seconds", 6.0))
    chunk_samples = int(sample_rate * chunk_seconds)
    input_device = listener_cfg.get("input_device")
    model_id = listener_cfg.get("parakeet_model", "mlx-community/parakeet-tdt-0.6b-v3")

    transcript_path = config.transcript_path
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    transcriber = ParakeetTranscriber(model_id)

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
    _own_stop = stop_event is None
    if stop_event is None:
        stop_event = threading.Event()

    def _on_sigint(*_: Any) -> None:
        log.info("stopping listener...")
        stop_event.set()  # type: ignore[union-attr]

    if _own_stop:
        signal.signal(signal.SIGINT, _on_sigint)
        signal.signal(signal.SIGTERM, _on_sigint)

    callback, _ = _make_callback(audio_q, sample_rate, chunk_samples)

    log.info(
        "listening: %dHz mono, %.1fs chunks, device=%s, → %s",
        sample_rate,
        chunk_seconds,
        input_device,
        transcript_path,
    )
    log.info("speak. ctrl-c to stop.")

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=input_device,
        callback=callback,
    ):
        while not stop_event.is_set():  # type: ignore[union-attr]
            try:
                chunk = audio_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                text = transcriber.transcribe_audio(chunk, sample_rate)
            except Exception as e:
                log.warning("transcription failed: %s", e)
                continue
            if not text:
                continue
            if _PAUSED.is_set():
                log.debug("listener: paused, discarding: %s", text[:40])
                continue
            record = {"t": now_iso(), "text": text}
            append_jsonl(transcript_path, record)
            log.info("» %s", text)

    return 0


def list_devices() -> int:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        print("sounddevice not installed.", file=sys.stderr)
        return 2
    print(sd.query_devices())
    return 0


def transcribe_file(config: Config, wav_path: Path) -> int:
    """Offline mode: transcribe a WAV file in chunks and write transcript.jsonl."""
    import wave

    listener_cfg = config.get("listener", default={}) or {}
    sample_rate = int(listener_cfg.get("sample_rate", 16000))
    chunk_seconds = float(listener_cfg.get("chunk_seconds", 6.0))
    model_id = listener_cfg.get("parakeet_model", "mlx-community/parakeet-tdt-0.6b-v3")
    chunk_samples = int(sample_rate * chunk_seconds)

    transcript_path = config.transcript_path
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    transcriber = ParakeetTranscriber(model_id)

    with wave.open(str(wav_path), "rb") as wf:
        wf_sr = wf.getframerate()
        wf_ch = wf.getnchannels()
        wf_sw = wf.getsampwidth()
        if wf_sw != 2:
            log.error("only 16-bit PCM WAV supported; got %d-byte samples", wf_sw)
            return 2
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if wf_ch > 1:
        audio = audio.reshape(-1, wf_ch).mean(axis=1)
    # Naive resample if rates differ — for serious use, use rubato or soxr.
    if wf_sr != sample_rate:
        import math

        ratio = sample_rate / wf_sr
        new_len = int(math.floor(len(audio) * ratio))
        audio = np.interp(
            np.linspace(0, len(audio), new_len, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)

    log.info("transcribing %s (%d samples) in %.1fs chunks", wav_path, len(audio), chunk_seconds)
    for i in range(0, len(audio), chunk_samples):
        chunk = audio[i : i + chunk_samples]
        if len(chunk) < chunk_samples // 4:
            break
        text = transcriber.transcribe_audio(chunk, sample_rate)
        if not text:
            continue
        record = {"t": now_iso(), "text": text}
        append_jsonl(transcript_path, record)
        log.info("» %s", text)

    return 0
