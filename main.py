"""Jarvis: hands-free voice assistant.

Wake word mode (default): Jarvis sleeps until he hears the wake phrase
("wake up, daddy's here"), then you just talk. Goodbye phrases ("call it a day",
"bye bye", ...) shut him down. Ctrl+C also quits.

Push-to-talk mode: press Enter to talk, Enter again to stop.
"""
import random
import re
import threading
import time
from datetime import datetime

import numpy as np
import yaml
from dotenv import load_dotenv

from jarvis import provenance
from jarvis.audio import Player, load_breath, record_until_enter
from jarvis.hud import HUD, NullHUD
from jarvis.llm import OllamaLLM, make_llm, sentence_chunks
from jarvis.phrases import Phrases
from jarvis.stt import STT
from jarvis.memory import Memory
from jarvis.semantic import CLAUDE_ROUTES, TOOL_ROUTES, SemanticRouter
from jarvis.tools import STANDBY_RE, Tools
from jarvis.tts import make_tts

PHRASE_PROMPT = """Live data: {facts}
The user asked: "{question}"
Answer in one or two short spoken sentences using only this data, in your usual dry British manner, and address the user as "sir".
Mention the conditions (rain, cloud, sun), not just numbers. Say temperatures as words, like "nine degrees"."""

GOODBYES = [
    "Very good, {t}. Shutting down.",
    "Rest well, {t}. Powering down now.",
    "As you wish, {t}. Goodbye for now.",
    "Signing off, {t}. It's been a pleasure.",
]
IDLE_SLEEP = "I'll be standing by if you need me, {t}."


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def greeting(title):
    hour = datetime.now().hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return random.choice([
        f"Good {part}, {title}. At your service.",
        f"Welcome back, {title}. What can I do for you?",
        f"Good {part}, {title}. Systems online and ready.",
    ])


# If what you said ends on one of these, you probably haven't finished the thought,
# so Jarvis waits a little longer for the rest instead of answering straight away.
TRAILING = {
    "and", "or", "but", "so", "because", "cause", "cuz", "is", "are", "was", "were", "am",
    "the", "a", "an", "to", "of", "for", "with", "like", "if", "then", "that", "which",
    "my", "your", "in", "on", "at", "about", "also", "what", "how", "i", "it's", "its",
    "um", "umm", "uh", "uhh", "ah", "ahh", "ahhh", "er", "erm", "hmm", "mm",
}


# ---------- is this a follow-up to what's on screen? ----------
# While web windows are open, each new question is checked: a follow-up keeps them
# (and a new search adds a window beside them); a different topic closes them.
STOP = {
    "the", "and", "for", "are", "was", "were", "what", "whats", "what's", "who", "how", "when", "where",
    "why", "which", "this", "that", "with", "from", "about", "have", "has", "had", "you", "your", "can",
    "could", "would", "should", "will", "tell", "know", "there", "they", "them", "its", "it's", "is",
    "please", "sir", "jarvis", "today", "now", "right", "any", "some", "does", "did", "do", "get", "give",
    "show", "find", "look", "search", "google", "me", "my", "our", "out", "into", "than", "then", "just",
    "like", "want", "need", "also", "more", "much", "many", "very", "really", "going", "gonna", "okay",
    "coming", "latest", "happening", "current", "currently", "thing", "things", "something", "anything",
}
FOLLOW_ON = re.compile(
    r"^(and|also|so|but|what about|how about|what else|tell me more|more on|more about|and what|"
    r"what if|then|okay and|ok and|really)\b|\b(this|that|it|they|them|those|these|he|she|his|her|their|its)\b"
)


def topic_words(text):
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 2 and w not in STOP}


def is_follow_up(text, topic):
    t = text.lower().strip()
    return bool(FOLLOW_ON.search(t)) or bool(topic_words(t) & topic)


def trails_off(text):
    t = text.strip().lower()
    if t.endswith(("...", "…", ",", "-")):   # whisper marks an unfinished sentence like this
        return True
    words = re.findall(r"[a-z']+", t)
    return bool(words) and words[-1] in TRAILING


# said while the local model loads back into the GPU after standing by...
BOOTING = [
    "Give me a few seconds, {t}, bringing the systems back online.",
    "One moment, {t}. Powering the core back up.",
    "Just a few seconds, {t}, while I bring everything online.",
    "Waking the systems, {t}. Bear with me a moment.",
    "Right away, {t}. Restoring full power, give me a few seconds.",
    "Hold on, {t}, getting the brain back up to speed.",
    "Spinning the systems up, {t}. Won't be a moment.",
    "Good to have you back, {t}. Give me a few seconds to come fully online.",
]
# ...and once it's ready
READY = [
    "All systems online, {t}. What can I do for you?",
    "I'm ready, {t}. What's your request?",
    "Fully operational, {t}. How may I help?",
    "Systems restored, {t}. What do you need?",
    "Back at full power, {t}. What shall we do?",
    "Ready when you are, {t}.",
    "Everything's up and running, {t}. How can I help?",
    "Online and at your service, {t}. What would you like?",
]
STANDING_BY = [
    "Standing by, {t}.",
    "Understood, {t}. I'll be here if you need me.",
    "Going quiet, {t}. Just say the word.",
    "Very good, {t}. Standing by.",
    "Of course, {t}. Enjoy, I'll stay out of the way.",
    "Powering down to standby, {t}.",
]


def main():
    load_dotenv()
    cfg = load_config()
    lcfg = cfg.get("listen") or {}
    mode = lcfg.get("mode", "wake_word")          # wake_word | push_to_talk
    on_goodbye = lcfg.get("on_goodbye", "exit")   # exit | sleep
    idle_sleep = lcfg.get("idle_sleep_seconds", 120)   # dozes off after 2 minutes of silence (0 = never)
    filler_wait = lcfg.get("filler_wait", 1.6)       # total pause allowed after "and", "um", "is"...

    name, title = cfg["assistant_name"], cfg["user_title"]
    hud = HUD(cfg, name) if (cfg.get("hud") or {}).get("enabled", True) else NullHUD()

    print("Loading speech recognition...")
    stt = STT(cfg["stt"])
    print("Loading voice...")
    tts = make_tts(cfg)
    memory = Memory(cfg)                              # memory.md notes + last conversation
    cfg["_memory_text"] = memory.notes_text()         # long-term notes go into both brains' instructions
    llm = make_llm(cfg)
    if hasattr(llm, "memory_fn"):
        llm.memory_fn = memory.notes_text             # the local brain always gets the latest notes
    if hasattr(llm, "notes_fn"):
        llm.notes_fn = memory.notes                   # Claude hears about notes saved or forgotten since startup
        llm.remote_notes = set(memory.notes())        # these are already in its instructions
    if isinstance(getattr(llm, "history", None), list):
        past = memory.load_history()                  # carry on from last time
        if past:
            llm.history = list(past)
            if hasattr(llm, "remote_seen"):
                llm.remote_seen = 0                   # Claude starts fresh, so it gets these too
            print(f"  (remembering the last {len(past) // 2} exchanges from last time)")
    web_brain = getattr(llm, "remote", llm)          # the Claude side does the web searches
    if hasattr(web_brain, "on_sources"):
        web_brain.on_sources = hud.sources            # show where answers came from in the HUD
    acfg = cfg["audio"]
    player = Player(
        pause_s=acfg.get("sentence_pause", 0.3),
        breath=load_breath(acfg.get("breath_file", "")),
        breath_chance=acfg.get("breath_chance", 0.3),
        hud=hud,
    )
    phrases = Phrases(lcfg)

    # Timers finish on a background thread, so voice synthesis is locked
    voice_lock = threading.Lock()

    def synth(text):
        with voice_lock:
            return tts.synthesize(text)

    def say(text):
        print(f"{name}: {text}")
        hud.reply(text)
        player.enqueue(*synth(text))

    def announce(text):
        print()
        say(text)

    tools = Tools(cfg, announce)
    tools.memory = memory
    semantic = SemanticRouter(cfg)   # picks the kind of request by meaning (falls back to keywords)
    # "do you remember...?" searches the notes by meaning, with the same embeddings
    memory.embed_fn = lambda texts: semantic._embed(texts)
    memory.history_fn = lambda: getattr(llm, "history", []) or []
    # The local model turns tool data (like weather) into a natural sentence.
    # Without one, the main brain does it.
    has_local = cfg["llm"]["provider"] in ("router", "ollama")
    phraser = OllamaLLM(cfg) if has_local else None

    # the local model leaves the GPU while Jarvis stands by, and comes back when he wakes
    local_models = [m for m in (getattr(llm, "local", None), llm, phraser)
                    if isinstance(m, OllamaLLM)]
    model_asleep = [False]

    def note_new_memories():
        """Let the local model write down anything new worth keeping (before it leaves the GPU)."""
        if local_models and isinstance(getattr(llm, "history", None), list):
            added = memory.extract(local_models[0], llm.history)
            if added:
                print("  (noted for good: " + "; ".join(added) + ")")

    def model_sleep():
        if local_models and not model_asleep[0]:
            model_asleep[0] = True

            def sleep_now():
                note_new_memories()
                local_models[0].unload()
            threading.Thread(target=sleep_now, daemon=True).start()

    def model_wake():
        """Reload in the background; say so while it loads, and say when it's ready."""
        def load():
            local_models[0].warm_up()
            model_asleep[0] = False
            player.wait()
            announce(random.choice(READY).format(t=title))
            hud.state("listening")
        say(random.choice(BOOTING).format(t=title))
        hud.state("booting")
        threading.Thread(target=load, daemon=True).start()

    topic = set()       # words from the questions behind the web windows that are open
    last_topic = set()  # the same for the windows that were closed last (to bring them back)

    def respond(text, t0, t_stt):
        hud.user(text)
        hud.state("thinking")
        sem = semantic.classify(text)             # (route, score) by meaning, or None
        if sem and sem[0] in TOOL_ROUTES:
            result = tools.handle(text, allow={sem[0]})   # only that kind of tool may answer
        elif sem:
            result = tools.handle(text, allow=set())      # a question for a brain: tools stay out (bar "which one?" answers)
            if not result and sem[0] in CLAUDE_ROUTES and hasattr(llm, "force_next"):
                llm.force_next = ("claude", f"meaning: {sem[0]}")
        else:
            result = tools.handle(text)                   # no clear meaning match: the keyword rules decide
        # web windows: keep them for a follow-up, close them for a new topic
        if result and result.tool in ("show", "close"):
            pass                                   # asking to see or close the windows: handled below
        elif hud.web_is_open():
            if is_follow_up(text, topic):
                hud.web_append = True          # a new search adds a window beside the others
                topic.update(topic_words(text))
            else:
                hud.web_close_all()
                last_topic.clear(); last_topic.update(topic)
                topic.clear()
        if not hud.web_is_open():
            hud.web_append = False
            topic.clear()
            topic.update(topic_words(text))   # in case this question opens windows
        if result and result.tool == "close":      # "close all tabs"
            was_open = hud.web_is_open() or bool(last_topic)
            hud.web_close_all()
            result.say = f"Closed, {title}." if was_open else f"There's nothing open, {title}."
        if result and result.tool == "show":       # "show me the pop-ups": bring the last pages back
            shown = hud.web_reshow()
            if shown and last_topic:
                topic.clear(); topic.update(last_topic)
            result.say = f"Here you are, {title}." if shown else f"I haven't looked anything up yet, {title}."
        if result and result.links:                # e.g. the news stories just read out: open them
            hud.sources("", result.links, [l["url"] for l in result.links[:3]])
            for l in result.links:
                topic.update(topic_words(l.get("title", "")))
        if result and result.tool == "ask_claude":   # "search it" after memory came up empty
            if hasattr(llm, "force_next"):
                llm.force_next = ("claude", "web search, memory had nothing")
            pieces = llm.stream(f"Search the web for this and answer it: {result.question}")
            result = None
        elif result and result.prompt:               # e.g. answering from memory notes
            pieces = phraser._stream(result.prompt) if phraser else llm.stream(result.prompt)
        elif result and result.facts:
            facts = provenance.data(f"tool:{result.tool}", result.facts) if provenance.enabled(cfg) else result.facts
            prompt = PHRASE_PROMPT.format(facts=facts, question=text)
            pieces = phraser._stream(prompt) if phraser else llm.stream(prompt)
        elif result:
            pieces = iter([result.say])
        else:
            pieces = llm.stream(text)

        print(f"{name}: ", end="", flush=True)
        first, said = True, []
        for sentence in sentence_chunks(pieces):
            print(sentence, end=" ", flush=True)
            said.append(sentence)
            hud.reply(" ".join(said))
            t_s = time.perf_counter()
            audio_out, sr = synth(sentence)
            if first:
                t_llm = t_s - t0 - t_stt          # waiting for the first full sentence (or tool)
                t_tts = time.perf_counter() - t_s  # turning it into audio
                total = time.perf_counter() - t0
                first = False
            player.enqueue(audio_out, sr)
        print()

        # the notes didn't answer it after all: the next answer gets saved
        if result and result.question and re.search(r"\b(?:don't|do not) (?:recall|see anything)\b", " ".join(said), re.I):
            tools.offer_teach(result.question)

        # Let the brain know what the tool said, so follow-up questions make sense
        if result and result.after:                # e.g. "keep scrolling": start only once he's stopped talking
            player.wait()
            result.after()

        if result and isinstance(getattr(llm, "history", None), list):
            done = f"(screen tool: {result.note}) " if result.note and result.tool != "memory" else \
                   (f"(memory: {result.note}) " if result.note else "")
            llm.history += [("user", text), ("assistant", (done + " ".join(said)).strip() or "(done)")]

        if isinstance(getattr(llm, "history", None), list):
            memory.save_history(llm.history)        # so a restart can carry on from here

        if not first:
            route = ("tool", result.tool) if result else getattr(llm, "last_route", None)
            brain = f" | {route[0]}: {route[1]}" if route else ""
            if sem:
                brain += f" | meaning {sem[0]} {sem[1]:.2f}"
            if route:
                hud.stats(route[0] if route[0] != "tool" else f"tool / {route[1]}", f"{total:.2f}s")
            print(
                f"  [stt {t_stt:.2f}s + brain {t_llm:.2f}s + voice {t_tts:.2f}s"
                f" = first audio {total:.2f}s{brain}]"
            )

    def transcribe(audio):
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        return text, t0, time.perf_counter() - t0

    try:
        if mode == "push_to_talk":
            print(f"\n{name} online. Press Enter to talk, Ctrl+C to quit.\n")
            say(f"{name} online. At your service, {title}.")
            player.wait()
            while True:
                hud.state("sleeping")
                input("[Enter] to talk ")
                hud.state("listening")
                text, t0, t_stt = transcribe(record_until_enter(acfg["input_rate"]))
                if not text:
                    print("(didn't catch that)")
                    continue
                print(f"You: {text}")
                if phrases.is_goodbye(text):
                    say(random.choice(GOODBYES).format(t=title))
                    player.wait()
                    break
                respond(text, t0, t_stt)
                player.wait()

        else:
            from jarvis.listener import Listener

            listener = Listener(acfg["input_rate"], player, lcfg, hud=hud)
            wake_hint = lcfg.get("wake_phrases", ["wake up, daddy's here"])[0]
            say(f"{name} online. At your service, {title}.")
            player.wait()
            hud.state("listening")
            print(f"\nAwake and listening. Say \"stand by\" to put me to sleep, \"{wake_hint}\" to wake me. Ctrl+C to quit.\n")
            awake = True                                   # starts awake; sleeps only when told to

            while True:
                audio = listener.next_utterance(timeout=idle_sleep if (awake and idle_sleep) else None)
                if audio is None:  # nobody spoke for a while
                    awake = False
                    say(IDLE_SLEEP.format(t=title))
                    player.wait()
                    model_sleep()
                    hud.state("sleeping")
                    print(f"\nSleeping. Say \"{wake_hint}\" to wake me.\n")
                    continue

                text, t0, t_stt = transcribe(audio)
                if not text:
                    continue

                # ended on "and", "um", "is"... : give them a moment to carry on
                while awake and trails_off(text):
                    waited = listener.silence_end + t_stt
                    more = listener.next_utterance(timeout=max(0.3, filler_wait - waited))
                    if more is None:
                        break                                  # they really were done
                    print(f"  (heard \"{text}\", you carried on)")
                    audio = np.concatenate([audio, more])
                    text, t0, t_stt = transcribe(audio)        # transcribe it all together for accuracy

                if not awake:
                    if phrases.is_wake(text):
                        awake = True
                        print(f"You: {text}")
                        hud.user(text)
                        if model_asleep[0]:
                            model_wake()          # reloads in the background, then says it's ready
                        else:
                            say(greeting(title))
                            player.wait()
                            hud.state("listening")
                        print("  (awake, just talk)")
                    else:
                        print(f"  (asleep, heard: {text})")
                    continue

                print(f"You: {text}")
                if STANDBY_RE.search(text.lower()):     # "stand by": sleep until the wake phrase
                    hud.user(text)
                    say(random.choice(STANDING_BY).format(t=title))
                    player.wait()
                    awake = False
                    model_sleep()
                    hud.state("sleeping")
                    print(f"\nStanding by. Say \"{wake_hint}\" to wake me.\n")
                    continue
                if phrases.is_goodbye(text):
                    hud.user(text)
                    say(random.choice(GOODBYES).format(t=title))
                    player.wait()
                    if on_goodbye == "exit":
                        print(f"\n{name} shutting down.")
                        break
                    awake = False
                    model_sleep()
                    hud.state("sleeping")
                    print(f"\nSleeping. Say \"{wake_hint}\" to wake me.\n")
                    continue

                respond(text, t0, t_stt)
                player.wait()
                hud.state("listening")

    except (KeyboardInterrupt, EOFError):
        print(f"\nGoodbye, {title}.")
    finally:
        if isinstance(getattr(llm, "history", None), list):
            memory.save_history(llm.history)
            if not model_asleep[0]:
                print("Saving anything worth remembering...")
                note_new_memories()
        hud.state("offline")
        time.sleep(0.3)  # let the HUD show OFFLINE before the server stops
        hud.close()      # shuts the small web window if one is open
        if hasattr(llm, "close"):
            llm.close()
        elif hasattr(getattr(llm, "remote", None), "close"):
            llm.remote.close()


if __name__ == "__main__":
    main()
