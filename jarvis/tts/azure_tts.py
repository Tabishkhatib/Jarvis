import os
from xml.sax.saxutils import escape

import requests

from jarvis.tts.base import TTSProvider, pcm16_to_float


class AzureTTS(TTSProvider):
    SAMPLE_RATE = 24000

    def __init__(self, cfg):
        self.key = os.environ["AZURE_SPEECH_KEY"]
        self.voice = cfg.get("azure_voice", "en-GB-RyanNeural")
        region = cfg.get("azure_region", "uksouth")
        self.url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"

    def synthesize(self, text):
        ssml = (
            '<speak version="1.0" xml:lang="en-GB">'
            f'<voice name="{self.voice}">{escape(text)}</voice></speak>'
        )
        r = requests.post(
            self.url,
            headers={
                "Ocp-Apim-Subscription-Key": self.key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
                "User-Agent": "jarvis",
            },
            data=ssml.encode("utf-8"),
            timeout=30,
        )
        r.raise_for_status()
        return pcm16_to_float(r.content), self.SAMPLE_RATE
