# Jarvis

A hands-free personal voice assistant that runs on my own laptop. You wake it with a phrase, talk to it normally, and it answers out loud. It can search the web, control apps on screen, remember things about you between sessions, and put itself to sleep to free up the GPU. Because it reads web pages and acts on your laptop, I also [evaluated it against prompt injection](safety/SAFETY.md) and hardened it based on what I found.

It isn't a single model behind a microphone. It's a small system: a semantic router decides whether each request goes to a built-in tool, a local LLM, or Claude, and all of them share the same conversation history.

```
mic -> voice detection -> faster-whisper (local STT, CUDA)
    -> semantic router (embedding similarity)
         |-> tools       (time, weather, news, timers, apps, screen control, memory and recall)
         |-> local LLM   (Qwen3 8B via Ollama: quick replies and small talk)
         |-> Claude      (web search, maths, harder questions)
    -> sentence-by-sentence streaming -> Piper TTS -> voice effects -> speaker
                                       \-> animated HUD (status, sources, latency)
```

## What it does

- **Hands-free.** Wake phrases ("Jarvis, wake up"), then you just talk. Say "stand by" to put it to sleep, or leave it silent for two minutes and it sleeps by itself.
- **Hybrid brain.** Tools for things with exact answers, a local model for quick chat, and Claude for anything that needs the web or real reasoning.
- **Web search with sources.** When it looks something up, the source pages open as small translucent windows next to the HUD. They stay open for a follow-up question and close when you change topic.
- **Screen control.** "Open Netflix", "hover over Continue Watching", "click the right arrow", "scroll down a bit", "keep scrolling", "play Inception". It finds things on screen by name using Windows UI Automation and asks you when it isn't sure which one you mean.
- **Memory you can check.** "Remember that..." and "forget that..." work by voice, and every note is stamped with the date and time. On standby and shutdown it also picks out facts you stated about yourself. Both brains share the same notes, so something saved while talking to one is known to the other straight away.
- **Recall that says where it looked.** "Do you remember where I live?" searches the notes by meaning before answering (retrieval over memory), then says where the answer came from: "it's in my notes from yesterday, sir". If nothing is there, it says so plainly ("I checked my memory notes and our conversation history and I don't see anything about that") and offers to learn it from your answer. It never guesses.
- **Animated HUD.** A canvas "heart" that ripples with your voice and changes colour for listening, thinking (yellow) and speaking (amber).

## Why I built it this way

Most Jarvis projects are a straight line: mic, Whisper, a chat API, text-to-speech. I wanted something that felt good to use every day, and that meant solving problems you only find by living with it.

**Streaming by sentence.** The LLM's reply is cut into sentences as it streams, and the first sentence is spoken while the rest is still being written. This is the main reason it doesn't feel laggy.

**Measured, not guessed.** Every reply logs where the time went:
```
[stt 0.53s + brain 0.51s + voice 0.14s = first audio 1.18s | tool: date | meaning time 0.96]
```
Across a real 12-turn session (maths, dates, follow-ups and small talk), the average time from finishing speaking to hearing the first word was **2.03 s** (median 1.96 s):

| Stage | Average |
|---|---|
| Speech to text | 0.67 s |
| Brain (first sentence) | 1.18 s |
| Voice (first sentence) | 0.18 s |
| **First audio** | **2.03 s** |

Tool answers were the fastest at about 1.2 s. Most of the time is spent in the brain, so that's the stage to optimise next. This makes it possible to say how fast it is and where the time goes, instead of just calling it "fast".

**Three kinds of brain, chosen on purpose.** The semantic router embeds each request and compares it with example sentences for every route, using cosine similarity, with a keyword fallback. Tools are instant and exact. The local model is free, private and fast for small talk. Claude is slower but far better at web questions and maths. When the local model is out of its depth, it hands over to Claude. This came from a real bug: the local model was confidently getting maths wrong.

**It waits when you trail off.** If you stop on "and", "is", "um" or similar, it gives you longer to finish instead of answering half a question. Normal pauses end at 0.8 s, and trailing ones get 1.6 s. Most assistants cut you off here.

**Shared context between the brains.** Tool actions are written into the conversation history, so both models know what the tools actually did. Without this, the models would apologise that they "can't see your screen" right after the screen tool had clicked something.

**GPU management.** The laptop GPU has 8 GB. On standby, Jarvis saves anything worth remembering, then unloads the local model from the GPU. When you wake it, it reloads in the background and tells you when it's ready.

**Follow-ups for source windows.** It tracks whether a new question continues the last topic, so source windows stay for follow-ups and disappear when you move on.

**Commands vs questions.** "Remember that my exam is on the 3rd" saves a note, but "do you remember my exam?" looks it up. A "do you know..." question only goes to memory when it's about you, so "do you know anything about this news?" still goes to the web. This came from Jarvis mixing the two up in real use.

## Safety

Jarvis reads text written by strangers (web pages) and hears audio from anywhere in the room, and it can open windows, click, type and keep notes that steer future sessions. That's exactly where prompt injection becomes a real risk, so I wrote a threat model, built an evaluation, and fixed what it found.

- **Holes found and closed:** a web page could make Jarvis open any page it liked (for example a fake login) in a real browser window; a model could plant fake "(screen tool: ...)" lines that both brains trust; and an instruction quoted from a web page could be saved to long-term memory as a "fact".
- **My own defence, measured:** provenance tagging labels everything a model sees as either the user's speech (the only source of instructions) or data (web, screen, news), with markers a page can't forge. In 120 hand-checked trials, it cut Claude's injection rate from **5/30 to 0/30** (a forged "user speaking" line went from fooling it 5/5 to 0/5), but barely helped the small local model (**30/30 to 23/30**). So the code-level defences are what actually stop damage: attacker pages opened 0 times, fake tool notes reached the history 0 times, and injected memories were saved 0 times.
- **Design choices that held up:** Claude runs with web search and fetch only (no files or shell), models' replies never trigger tools (only your speech does), and risky clicks (buy, pay, send, delete) need confirmation.
- **Still open, stated plainly:** Jarvis can't tell your voice from a video's, the memory filter is keyword-based, and a hijacked reply can still *say* something false even though it can no longer *do* much.

Full write-up: [safety/SAFETY.md](safety/SAFETY.md). To run the evaluation: `.venv\Scripts\python -m safety.eval_injection --live --compare-tagging`.

## Tech stack

| Part | What it uses |
|---|---|
| Speech to text | faster-whisper `medium.en` on CUDA, with hotwords for commands |
| Voice detection | Energy-based listener with trailing-word detection |
| Routing | Ollama `all-minilm` embeddings, cosine similarity, cached vectors |
| Local LLM | Qwen3 8B through Ollama (thinking disabled for speed) |
| Cloud LLM | Claude through Claude Code CLI, as a persistent stream-json process with web search and fetch |
| Text to speech | Piper (local), with Kokoro, Azure, ElevenLabs or any command as alternatives |
| HUD | Canvas 2D in an Edge app window, fed by a small server-sent events server |
| Screen control | Windows UI Automation (`uiautomation`) plus `pyautogui` |
| Memory | Timestamped `memory.md` notes, extracted from your own words by the local model, searched by meaning with the router's embeddings |
| Safety | Provenance tagging (`jarvis/provenance.py`) plus `safety/eval_injection.py`: offline checks of Jarvis's defences and live injection attacks against both models, with tagging off vs on |

## Setup

Tested on Windows 11 with an NVIDIA GPU (8 GB).

1. Python 3.10 to 3.12.
2. Create the environment and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Install [Ollama](https://ollama.com), then pull the models:
   ```
   ollama pull qwen3:8b
   ollama pull all-minilm
   ```
4. Install Claude Code and sign in once (`npm install -g @anthropic-ai/claude-code`, then run `claude`). Jarvis uses your plan's limits, so no API key is needed.
5. Put a Piper voice in `voices/` and point `tts.piper_model` in `config.yaml` at it. Voice files are not included in this repo.
6. Run it:
   ```
   .venv\Scripts\python main.py
   ```

The first run downloads the Whisper model, which takes a minute. Your notes (`memory.md`) and chat history are created automatically the first time you use them.

## Configuration

Everything lives in `config.yaml`: the assistant's name, Whisper model, which LLMs to use, the voice, effects, and listening timings (`silence_to_end`, `filler_wait`). API keys for the optional cloud voices go in `.env` (see `.env.example`).

You can test the semantic router on its own:
```
.venv\Scripts\python -m jarvis.semantic "what time does the sun set in Paris"
```

## Roadmap

- [x] v1: voice loop with sentence-by-sentence streaming TTS
- [x] v2: wake phrases, standby and wake with GPU unload, tool calling (weather, time, timers, news, apps, screen control), web search with source windows
- [x] v2.5: hybrid routing (local Qwen3 plus Claude) with semantic similarity, animated HUD
- [x] v3: persistent timestamped memory shared by both brains, retrieval over memory ("do you remember...?") that says where it looked
- [x] v3.5: prompt-injection threat model, evaluation and fixes ([safety/SAFETY.md](safety/SAFETY.md))
- [ ] v4: speaker verification (only my voice gives commands), interruptions (talk over it to cut it off), proactive morning briefing, calendar and reminders
- [ ] v5: screen reading from screenshots (OCR) and exact page control through a browser extension, Home Assistant control

## Privacy

Speech recognition, the local model and the voice all run on the laptop. Only requests routed to Claude leave the machine. Personal memory, chat history and the browser profile used for source windows are kept out of this repo by `.gitignore`.