"""Piper voices: a .onnx model plus its matching .onnx.json config."""
import os

import numpy as np

from jarvis.tts.base import TTSProvider


class PiperTTS(TTSProvider):
    def __init__(self, cfg):
        from piper import PiperVoice
        from piper.config import SynthesisConfig

        model = cfg.get("piper_model", "")
        if not model or not os.path.exists(model):
            raise FileNotFoundError(f"Piper model not found: {model!r}")
        if not os.path.exists(model + ".json"):
            raise FileNotFoundError(
                f"Missing {model}.json. Piper needs the config file next to the model."
            )
        self.voice = PiperVoice.load(model, use_cuda=cfg.get("piper_cuda", False))
        self.syn = SynthesisConfig(
            length_scale=cfg.get("piper_length_scale"),   # higher = slower
            noise_scale=cfg.get("piper_noise_scale"),     # higher = more expressive pitch
            noise_w_scale=cfg.get("piper_noise_w"),       # higher = more varied rhythm
        )
        self.sr = self.voice.config.sample_rate
        # pause between sentences Piper splits internally (e.g. "Yes. Right away.")
        self.gap = np.zeros(int(self.sr * cfg.get("piper_inner_pause", 0.15)), dtype=np.float32)

    def synthesize(self, text):
        parts = []
        for chunk in self.voice.synthesize(text, syn_config=self.syn):
            if parts:
                parts.append(self.gap)
            parts.append(chunk.audio_float_array.astype(np.float32))
        if not parts:
            return np.zeros(0, dtype=np.float32), self.sr
        return np.concatenate(parts), self.sr
