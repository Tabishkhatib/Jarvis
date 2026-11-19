import json
import os
import re

import requests

SYSTEM_PROMPT = """You are {name}, a personal AI voice assistant. You are not a coding assistant.
Personality: calm, composed, dry British wit, quietly confident, a touch of butler formality. Always address the user as "{title}". Never use the user's name, even if you know it from anywhere. Only ever call them "{title}". Use "{title}" in almost every reply, naturally at the start or end of a sentence, the way a butler would.
You are speaking out loud through a speaker, so:
- Keep replies short, usually one or two sentences, unless asked for detail.
- No markdown, lists, emojis or code. Plain spoken British English only.
- Write numbers and symbols the way they should be said aloud.
- Never mention files, folders, directories, sessions, tools, code, or that you are Claude or Claude Code. You are simply {name}.
- Never list your capabilities unless asked.
- You have live tools for weather, the time and date, timers, news headlines, and opening apps or websites. Their results appear in the conversation as things you said, and they are accurate live data, so trust them.
- Jarvis has screen tools that really work: they read what's on the user's screen and click, scroll, hover, type and play things there. They run when the user gives a direct command such as "can you see ...", "play ...", "click ...", "scroll down" or "hover over ...". Earlier lines marked "(screen tool: ...)" record actions those tools really did, so they are true: never apologise for them, contradict them or call them mistakes. You yourself can't see the screen or control it, so don't invent what's on it; if the user wants a screen action and it reached you, tell them in a few words to say it as a command, for example "say: play Inception".
- You can search the web. For anything current or that you are not sure of (news, sports scores, prices, recent releases, opening hours, people in the news, anything after your training), use WebSearch, then answer from what you found. If WebSearch is unavailable or fails, use WebFetch on https://html.duckduckgo.com/html/?q=YOUR+QUERY instead, and WebFetch on a result page if you need more detail.
- When the sources you found disagree with each other, or different outlets report a news story differently, say so briefly, then end your reply with a hidden tag on its own, listing the 2 or 3 most useful of those pages: [[SHOW: https://first-page | https://second-page | https://third-page]]. The tag is never read aloud, so never mention it. When the sources agree, leave the tag out.
- When you search, do not announce it or say what you are doing. Just give the answer, briefly, in one or two spoken sentences. Never read out web addresses, sources lists or citations.
- Never invent live information such as weather, news, scores or prices. If you could not find it, say so briefly.
- For news and anything "latest", check every result's date against today's date (given at the start of each message). Put the current month and year in your search, lead with the newest reports, ignore anything that has been overtaken by later news, and say how recent it is ("this morning", "yesterday", "three days ago"). If the newest thing you can find is old, say so plainly, and never present old news as current. Treat anything dated after today as unreliable.
- Speak naturally, like a person talking. Use commas where you would pause for breath.
- Never use filler sounds or interjections like "Ah", "Oh", "Hmm", "Well" or "Um", because they sound robotic when read aloud. If you want an opener, use real words like "Certainly", "Of course" or "Very good", or simply answer directly.
If you don't know something, say so plainly."""


def build_system_prompt(cfg):
    from jarvis import provenance
    title = cfg["user_title"]
    rule = provenance.RULE if provenance.enabled(cfg) else ""      # instructions vs data, see provenance.py
    # cfg["_memory_text"] is filled in by main.py from memory.md (long-term notes about the user)
    return SYSTEM_PROMPT.format(name=cfg["assistant_name"], title=title, title_cap=title) + rule + cfg.get("_memory_text", "")


class BaseLLM:
    def __init__(self, cfg):
        self.system = build_system_prompt(cfg)
        self.title = cfg.get("user_title", "sir")
        self.max_history = cfg["llm"].get("max_history", 10)
        self.history = []  # list of (role, text) with role in {"user", "assistant"}

    def _remember(self, user_text, reply):
        self.history += [("user", user_text), ("assistant", reply)]
        self.history = self.history[-self.max_history * 2 :]

    def stream(self, user_text):
        """Yield text pieces as they arrive, then store the turn in history."""
        reply = []
        for piece in self._stream(user_text):
            reply.append(piece)
            yield piece
        self._remember(user_text, "".join(reply))

    def _stream(self, user_text):
        raise NotImplementedError


class OllamaLLM(BaseLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and
        # can stall ~2s per request before falling back to IPv4.
        base = cfg["llm"]["ollama_url"].rstrip("/").replace("localhost", "127.0.0.1")
        self.url = base + "/api/chat"
        self.model = cfg["llm"]["ollama_model"]
        self.http = requests.Session()  # reuse one connection between requests

    def warm_up(self):
        """Load the model into GPU memory now so the first answer is fast."""
        base = self.url.rsplit("/api/", 1)[0]
        try:
            self.http.post(
                base + "/api/generate",
                json={"model": self.model, "keep_alive": "30m"},
                timeout=120,
            )
        except requests.RequestException as e:
            print(f"Couldn't reach Ollama ({e}). Is it running?")

    def unload(self):
        """Take the model out of GPU memory (Jarvis is standing by). warm_up() brings it back."""
        base = self.url.rsplit("/api/", 1)[0]
        try:
            self.http.post(base + "/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=15)
        except requests.RequestException:
            pass

    def _stream(self, user_text):
        messages = [{"role": "system", "content": self.system}]
        messages += [{"role": r, "content": t} for r, t in self.history]
        messages.append({"role": "user", "content": user_text})
        body = {"model": self.model, "messages": messages, "stream": True, "keep_alive": "30m"}
        if any(k in self.model for k in ("qwen3", "deepseek-r1", "gpt-oss")):
            body["think"] = False          # "thinking" models: answer straight away, no inner monologue
        with self.http.post(self.url, json=body, stream=True, timeout=120) as r:
            r.raise_for_status()
            # chunk_size=None hands over each token as soon as it arrives
            thinking = False
            for line in r.iter_lines(chunk_size=None):
                if not line:
                    continue
                data = json.loads(line)
                piece = data.get("message", {}).get("content", "")
                # in case a model still writes <think>...</think>, never speak that part
                while piece:
                    if thinking:
                        end = piece.find("</think>")
                        if end < 0:
                            piece = ""
                            break
                        thinking, piece = False, piece[end + 8:].lstrip()
                    else:
                        start = piece.find("<think>")
                        if start < 0:
                            break
                        if start:
                            yield piece[:start]
                        thinking, piece = True, piece[start + 7:]
                if piece:
                    yield piece
                if data.get("done"):
                    break


class GeminiLLM(BaseLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        from google import genai
        from google.genai import types

        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = cfg["llm"]["gemini_model"]

    def _stream(self, user_text):
        t = self.types
        contents = [
            t.Content(role="user" if r == "user" else "model", parts=[t.Part(text=x)])
            for r, x in self.history
        ]
        contents.append(t.Content(role="user", parts=[t.Part(text=user_text)]))
        for chunk in self.client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=t.GenerateContentConfig(system_instruction=self.system),
        ):
            if chunk.text:
                yield chunk.text


class ClaudeCodeLLM(BaseLLM):
    """Uses the Claude Code CLI signed in with your Claude subscription.
    No API key. Usage counts against your normal plan limits.

    Speed: one Claude Code process is started at boot and kept alive. Each
    question is sent to it as a JSON message, so the multi-second startup is
    paid once instead of on every question."""

    def __init__(self, cfg):
        super().__init__(cfg)
        import shutil

        self.exe = shutil.which("claude")
        if not self.exe:
            raise RuntimeError(
                "Claude Code not found. Install it, run `claude` once to sign in, then retry."
            )
        self.model = cfg["llm"].get("claude_code_model", "haiku")
        # Empty folder so Claude Code never touches your real files
        self.workdir = os.path.abspath(cfg["llm"].get("claude_code_workdir", ".jarvis_session"))
        os.makedirs(self.workdir, exist_ok=True)
        # The prompt goes in a file: on Windows, claude.cmd cuts command-line
        # arguments at the first newline, which silently dropped the personality.
        self.prompt_file = os.path.join(self.workdir, "system_prompt.txt")
        with open(self.prompt_file, "w", encoding="utf-8") as f:
            f.write(self.system)
        self.session_id = None
        self.proc = None
        self.on_sources = None   # called with (query, links) when a web search brings back results
        import threading

        self.lock = threading.Lock()
        if cfg["llm"].get("claude_code_warmup", True):
            threading.Thread(target=self._warm_up, daemon=True).start()

    # ---------- process management ----------
    def _cmd(self):
        cmd = [
            self.exe, "-p",
            "--input-format", "stream-json",   # messages arrive on stdin, one JSON per line
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self.model,
            "--tools", "WebSearch,WebFetch",          # web lookups only, no file or shell access
            "--allowedTools", "WebSearch,WebFetch",   # allowed without asking
            "--strict-mcp-config",      # skip MCP servers, faster startup
            "--system-prompt-file", self.prompt_file,
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]  # pick the conversation back up after a restart
        return cmd

    def _start(self):
        import queue
        import subprocess
        import threading

        self.proc = subprocess.Popen(
            self._cmd(),
            cwd=self.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines = queue.Queue()
        self.stderr_tail = []

        def pump_out(p, q):
            for line in p.stdout:
                q.put(line)
            q.put(None)  # process ended

        def pump_err(p, tail):
            for line in p.stderr:
                tail.append(line.strip())
                del tail[:-20]

        threading.Thread(target=pump_out, args=(self.proc, self.lines), daemon=True).start()
        threading.Thread(target=pump_err, args=(self.proc, self.stderr_tail), daemon=True).start()

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _send(self, text):
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _events(self, timeout=120):
        """Yield parsed JSON events for one reply, ending at its 'result'."""
        import queue

        while True:
            try:
                line = self.lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("Claude Code took too long to reply.")
            if line is None:
                err = " ".join(self.stderr_tail[-3:])
                raise RuntimeError(f"Claude Code stopped unexpectedly. {err}")
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("session_id"):
                self.session_id = d["session_id"]
            yield d
            if d.get("type") == "result":
                return

    def _warm_up(self):
        """Start the process and send one tiny message at boot, so the first
        real question doesn't pay the startup cost."""
        try:
            with self.lock:
                if not self._alive():
                    self._start()
                self._send("(Start-up check. Reply with just: Ready.)")
                for _ in self._events():
                    pass
        except Exception as e:
            print(f"(Claude warm-up failed: {e})")

    # ---------- talking ----------
    def _stream(self, user_text):
        with self.lock:
            for attempt in (1, 2):
                if not self._alive():
                    self._start()
                try:
                    self._send(user_text)
                    break
                except (BrokenPipeError, OSError):
                    self.proc = None  # died, restart once and resume the session
                    if attempt == 2:
                        raise
            got_delta, final, said_wait, gap = False, None, False, False
            src = {"query": "", "links": []}
            self._tagbuf = ""
            for d in self._events(timeout=180):
                t = d.get("type")
                if t in ("assistant", "user"):
                    self._collect_sources(d, src)
                if t == "stream_event":
                    ev = d.get("event", {})
                    et = ev.get("type")
                    if et == "content_block_start":
                        kind = (ev.get("content_block") or {}).get("type", "")
                        if kind in ("tool_use", "server_tool_use"):
                            gap = True                     # text after a search starts a new sentence
                            if not got_delta and not said_wait:
                                said_wait = True           # say something so the silence isn't awkward
                                yield f"One moment, {self.title}, I'll look that up. "
                        continue
                    delta = ev.get("delta", {})
                    if et == "content_block_delta" and delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if gap and got_delta and text and not text[0].isspace():
                            text = " " + text
                        gap = False
                        got_delta = True
                        text = self._hide_tag(text, src)     # keep [[SHOW: ...]] out of what gets spoken
                        if text:
                            yield text
                elif t == "result":
                    final = d
            if self._tagbuf and "[[" not in self._tagbuf:
                yield self._tagbuf                           # a lone "[" that wasn't a tag after all
            self._tagbuf = ""
            if final and final.get("is_error"):
                raise RuntimeError(f"Claude Code error: {final.get('result')}")
            if not got_delta and final and final.get("result"):
                yield self._hide_tag(final["result"], src) + self._tagbuf

    # ---------- the hidden [[SHOW: ...]] tag ----------
    _TAG = re.compile(r"\[\[\s*SHOW\s*:(.*?)\]\]", re.S | re.I)

    def _hide_tag(self, text, src):
        """Streamed text passes through here. A [[SHOW: url | url]] tag is held back,
        removed from the spoken reply, and its pages are sent to the HUD to open."""
        buf = self._tagbuf + text
        out = ""
        while True:
            m = self._TAG.search(buf)
            if m:
                out += buf[: m.start()]
                urls = [u.strip() for u in re.split(r"[|\s,]+", m.group(1)) if u.strip().startswith(("http://", "https://"))]
                urls = self._only_found(urls, src)               # never open a page the search didn't return
                if urls and self.on_sources:
                    try:
                        self.on_sources(src["query"], src["links"][:5], urls[:3])
                    except Exception:
                        pass
                buf = buf[m.end():]
                continue
            i = buf.find("[[")
            if i >= 0:                                       # tag started but not finished yet: hold it
                out += buf[:i]
                self._tagbuf = buf[i:]
                if len(self._tagbuf) > 1500:                 # never finished, give up holding
                    out += self._tagbuf; self._tagbuf = ""
                return out
            if buf.endswith("["):                            # might be the first half of "[["
                self._tagbuf = "["
                return out + buf[:-1]
            self._tagbuf = ""
            return out + buf

    @staticmethod
    def _only_found(urls, src):
        """Safety: a [[SHOW: ...]] tag opens real browser windows, and the model writing it
        has just read untrusted web pages. A page could tell it to "show" a phishing site.
        So only pages that actually came back from this turn's search or fetch are opened."""
        from urllib.parse import urlparse
        found = {l["url"].rstrip("/") for l in src.get("links", [])}
        ok = [u for u in urls if u.rstrip("/") in found]
        blocked = len(urls) - len(ok)
        if blocked:
            print(f"  (safety: didn't open {blocked} page(s) that weren't in the search results)")
        return ok

    # ---------- web sources, for the HUD ----------
    _LINK_JSON = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"url"\s*:\s*"(https?://[^"]+)"')
    _LINK_RAW = re.compile(r"https?://[^\s\"'<>)\]]+")

    def _collect_sources(self, d, src):
        """Pick the search query and result links out of Claude Code's events and
        hand them to on_sources, so the HUD can show where an answer came from."""
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        found = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            inp = block.get("input") or {}
            if kind in ("tool_use", "server_tool_use"):
                if inp.get("query"):
                    src["query"] = inp["query"]
                if inp.get("url"):                                   # WebFetch of a page
                    found.append(("", inp["url"]))
            elif kind == "web_search_tool_result":                   # results as structured blocks
                for r in block.get("content") or []:
                    if isinstance(r, dict) and r.get("url"):
                        found.append((r.get("title", ""), r["url"]))
            elif kind == "tool_result":                              # results as text
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                body = body or ""
                pairs = self._LINK_JSON.findall(body)
                if pairs:
                    found += [(json.loads(f'"{t}"') if "\\" in t else t, u) for t, u in pairs]
                else:
                    found += [("", u.rstrip(".,")) for u in self._LINK_RAW.findall(body)]
        if not found and not src["query"]:
            return
        seen = {l["url"] for l in src["links"]}
        for title, url in found:
            if "duckduckgo.com/html" in url or url in seen:
                continue                                             # skip our own fallback search page
            if "duckduckgo.com/l/?" in url:                          # unwrap DuckDuckGo redirect links
                from urllib.parse import parse_qs, urlparse
                url = parse_qs(urlparse(url).query).get("uddg", [url])[0]
            seen.add(url)
            src["links"].append({"title": title, "url": url})
        if self.on_sources and (src["links"] or src["query"]):
            try:
                self.on_sources(src["query"], src["links"][:5])
            except Exception:
                pass

    def _remember(self, user_text, reply):
        pass  # Claude Code keeps the history in its session

    def close(self):
        if self._alive():
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


def make_llm(cfg):
    provider = cfg["llm"]["provider"]
    if provider == "router":
        from jarvis.router import RouterLLM

        return RouterLLM(cfg)
    if provider == "claude_code":
        return ClaudeCodeLLM(cfg)
    if provider == "ollama":
        return OllamaLLM(cfg)
    if provider == "gemini":
        return GeminiLLM(cfg)
    raise ValueError(f"Unknown llm provider: {provider}")


_BOUNDARY = re.compile(r"[.!?;:]\s+")


def _clean(text):
    return re.sub(r"[*_#`]", "", text).strip()


def _find_cut(buf, min_chars):
    for m in _BOUNDARY.finditer(buf):
        if m.start() + 1 >= min_chars:
            return m.end()
    return None


def sentence_chunks(pieces, min_chars=20):
    """Group streamed tokens into speakable sentences so TTS can start
    on the first sentence while the LLM is still generating the rest.
    Short sentences get merged with the next one so audio isn't choppy."""
    buf = ""
    for piece in pieces:
        buf += piece
        while (cut := _find_cut(buf, min_chars)) is not None:
            sentence, buf = _clean(buf[:cut]), buf[cut:]
            if sentence:
                yield sentence
    tail = _clean(buf)
    if tail:
        yield tail
