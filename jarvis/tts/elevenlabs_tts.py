import os

import requests

from jarvis.tts.base import TTSProvider, pcm16_to_float


class ElevenLabsTTS(TTSProvider):
    SAMPLE_RATE = 24000

    def __init__(self, cfg):
        self.key = os.environ["ELEVENLABS_API_KEY"]
        self.voice_id = cfg["elevenlabs_voice_id"]
        self.model = cfg.get("elevenlabs_model", "eleven_flash_v2_5")
        if not self.voice_id:
            raise ValueError("Set tts.elevenlabs_voice_id in config.yaml")

    def synthesize(self, text):
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": self.key},
            json={"text": text, "model_id": self.model},
            timeout=30,
        )
        r.raise_for_status()
        return pcm16_to_float(r.content), self.SAMPLE_RATE
