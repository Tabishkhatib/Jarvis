import glob
import os
import sys


def _add_nvidia_dlls():
    """On Windows, make the pip-installed CUDA libraries (nvidia-cublas-cu12,
    nvidia-cudnn-cu12) findable, so no manual PATH editing is needed."""
    if os.name != "nt":
        return
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    for d in glob.glob(os.path.join(base, "*", "bin")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


_add_nvidia_dlls()

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


class STT:
    def __init__(self, cfg):
        self.hotwords = cfg.get(
            "hotwords",
            "Jarvis, wake up, daddy, weather, rain, forecast, umbrella, timer, remind me, open, YouTube, Spotify, "
            "Netflix, hover over, scroll down, scroll up, keep scrolling, stop, click, play, press play, "
            "right arrow, left arrow, again, stand by, go back, type",
        )
        # personal words (places, names) live in the private .env, never in the code on GitHub:
        # JARVIS_EXTRA_HOTWORDS=My Uni, My Town
        extra = os.environ.get("JARVIS_EXTRA_HOTWORDS", "").strip()
        if extra:
            self.hotwords = f"{self.hotwords}, {extra}"
        device, compute = cfg["device"], cfg["compute_type"]
        try:
            self.model = WhisperModel(cfg["model"], device=device, compute_type=compute)
            # Warm up now so a missing GPU library fails at startup, not mid-conversation
            segments, _ = self.model.transcribe(np.zeros(16000, dtype=np.float32), language="en")
            list(segments)
            print(f"Whisper running on {device}")
        except Exception as e:
            if device == "cpu":
                raise
            print(f"GPU failed ({e}), falling back to CPU")
            self.model = WhisperModel(cfg["model"], device="cpu", compute_type="int8")

    def transcribe(self, audio):
        if audio.size < 16000 * 0.3:  # under 0.3s, nothing useful
            return ""
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            # words Whisper should lean towards when the audio is unclear ("rain" not "train")
            hotwords=self.hotwords,
        )
        return " ".join(s.text.strip() for s in segments).strip()
