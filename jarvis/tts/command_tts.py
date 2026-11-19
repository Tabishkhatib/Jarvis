"""Generic slot for any TTS you run yourself.

Set tts.command in config.yaml, for example:
  command: "my-tts --input {text_file} --output {out_file}"
The command must write a .wav file to {out_file}.
"""
import os
import shlex
import subprocess
import tempfile

import numpy as np
import soundfile as sf

from jarvis.tts.base import TTSProvider


class CommandTTS(TTSProvider):
    def __init__(self, cfg):
        self.template = cfg.get("command", "")
        if not self.template:
            raise ValueError("Set tts.command in config.yaml")

    def synthesize(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            text_file = os.path.join(tmp, "in.txt")
            out_file = os.path.join(tmp, "out.wav")
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(text)
            cmd = self.template.format(text_file=text_file, out_file=out_file)
            subprocess.run(shlex.split(cmd, posix=os.name != "nt"), check=True)
            audio, sr = sf.read(out_file, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return np.asarray(audio, dtype=np.float32), sr
