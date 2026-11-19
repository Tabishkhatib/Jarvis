from jarvis.effects import apply_effects
from jarvis.tts.base import TTSProvider


class _WithEffects(TTSProvider):
    def __init__(self, inner, fx_cfg):
        self.inner, self.fx = inner, fx_cfg

    def synthesize(self, text):
        audio, sr = self.inner.synthesize(text)
        return apply_effects(audio, sr, self.fx), sr


def make_tts(cfg):
    tcfg = cfg["tts"]
    provider = tcfg["provider"]
    if provider == "kokoro":
        from jarvis.tts.kokoro_tts import KokoroTTS as cls
    elif provider == "piper":
        from jarvis.tts.piper_tts import PiperTTS as cls
    elif provider == "azure":
        from jarvis.tts.azure_tts import AzureTTS as cls
    elif provider == "elevenlabs":
        from jarvis.tts.elevenlabs_tts import ElevenLabsTTS as cls
    elif provider == "command":
        from jarvis.tts.command_tts import CommandTTS as cls
    else:
        raise ValueError(f"Unknown tts provider: {provider}")
    return _WithEffects(cls(tcfg), cfg.get("effects", {}))
