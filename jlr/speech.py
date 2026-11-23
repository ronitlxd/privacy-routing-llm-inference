"""Voice input: speech to text on the vehicle.

The driver screen records a short clip and sends it only to this server; Whisper (faster-whisper,
CPU, int8) turns it into text here. No audio or text goes to any other service, nothing is stored,
and the transcript is only put in the request box: the driver reads it and presses Send, so the
normal pipeline (routing, masking, confirmation) applies to it like typed text.
"""
import io
import os
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_AUDIO_BYTES = 2 * 1024 * 1024        # a 30 s Opus clip is well under 1 MB
MAX_SECONDS = 30
AUDIO_TYPES = ('audio/webm', 'audio/ogg', 'audio/wav', 'audio/x-wav', 'audio/mp4', 'audio/mpeg')
ENGINE = 'Whisper base.en on this vehicle'

_model = None
_lock = threading.Lock()


class SpeechError(Exception):
    """The clip cannot be transcribed (bad input); the message is shown to the driver."""


class SpeechUnavailable(SpeechError):
    """Voice input is not installed on this server."""


def model_path():
    configured = os.environ.get('JLR_SPEECH_MODEL')
    path = Path(configured) if configured else REPO / 'models' / 'whisper' / 'base.en'
    return path if path.is_absolute() else REPO / path


def available():
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return (model_path() / 'model.bin').is_file()


def load():
    """The Whisper model, loaded once (never downloaded here: it must already be on disk)."""
    global _model
    with _lock:
        if _model is None:
            if not available():
                raise SpeechUnavailable('Voice input is not set up on this vehicle.')
            from faster_whisper import WhisperModel
            threads = int(os.environ.get('JLR_SPEECH_THREADS', '4'))
            _model = WhisperModel(str(model_path()), device='cpu', compute_type='int8', cpu_threads=threads)
        return _model


def decode(audio):
    """16 kHz mono float32 samples from any clip PyAV can read (decoded here, independent of the
    faster-whisper helper, whose arguments differ between PyAV versions)."""
    import av
    import numpy as np
    resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
    parts = []
    with av.open(io.BytesIO(audio), mode='r') as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1))
    if not parts:
        raise ValueError('no audio')
    return np.concatenate(parts).astype(np.float32) / 32768.0


def transcribe(audio, content_type):
    """{'text', 'seconds', 'audio_seconds', 'engine'} for one recorded clip."""
    if content_type not in AUDIO_TYPES:
        raise SpeechError('Send the recording as audio (webm, ogg, wav or mp4).')
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise SpeechError('The recording is empty or too long.')
    model = load()
    started = time.monotonic()
    try:
        samples = decode(audio)
    except Exception:
        raise SpeechError('The recording could not be read.') from None
    audio_seconds = len(samples) / 16000
    if audio_seconds > MAX_SECONDS + 1:
        raise SpeechError(f'Keep voice requests under {MAX_SECONDS} seconds.')
    with _lock:  # one clip at a time: the CPU is shared with the local model
        segments, _ = model.transcribe(samples, language='en', beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False)
        text = ' '.join(s.text.strip() for s in segments).strip()
    return {'text': text, 'seconds': round(time.monotonic() - started, 2),
            'audio_seconds': round(audio_seconds, 1), 'engine': ENGINE}
