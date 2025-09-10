#!/usr/bin/env python3
from __future__ import annotations

import re, asyncio, ssl, time, base64, logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Deque
from collections import deque

import structlog
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai import APIError, APIStatusError, APIConnectionError, RateLimitError

from config import Settings

# --- load config early ---
load_dotenv(".env")
settings = Settings()

# --- logging ---
def setup_logging():
    processors = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if not settings.LOG_JSON else structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
    )
setup_logging()
log = structlog.get_logger()

# --- IRC limits ---
IRC_MAX_LINE = 510
PRIVMSG_PREFIX_OVERHEAD = 50
SEND_RATE_PER_SEC = 1.2

# --- Conversation memory config ---
CONTEXT_MAX_TURNS = 6          # keep up to 6 recent user↔assistant turns per nick
CONTEXT_TTL_SECONDS = 60 * 45  # expire memory after 45 minutes idle

# --- OpenAI client ---
aiclient = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_REQUEST_TIMEOUT)

SYSTEM_PROMPT = (
    "You are an IRC help-channel assistant for the Newnet IRC Network.\n"
    "\n"
    "Style:\n"
    "- Be concise, correct, and friendly. Plain text only. Keep answers ~6 lines unless asked for more.\n"
    "- Prefer pasteable commands for WeeChat/irssi/HexChat/mIRC. Never ask for secrets in-channel.\n"
    "\n"
    "Network defaults:\n"
    "- Primary connect host: irc.newnet.net (round-robin). Prefer TLS on 6697; 6667 is legacy.\n"
    "- If users report lag, suggest a nearby regional server from the current servers list.\n"
    "\n"
    "Location-based server hints (heuristics, not strict rules):\n"
    "- Western Canada / U.S. West / Pacific Rim (e.g., BC/AB/WA/OR/CA/AK/JP): vancouver-ca.newnet.net:6697.\n"
    "- Eastern Canada / U.S. East & Central: beauharnois-ca.newnet.net:6697.\n"
    "- South America: sao-paulo.newnet.net:6697.\n"
    "- Europe & Africa & Middle East: gravelines-fr.newnet.net:6697.\n"
    "- Australia / New Zealand / Oceania & much of SE/E Asia: australia-au.newnet.net:6697.\n"
    "- If unsure or traveling, start with irc.newnet.net:6697 and let DNS round-robin decide; switch to a regional host if latency stays high.\n"
    "\n"
    "Services quicktips (Anope):\n"
    "- NickServ: REGISTER <password> <email>, IDENTIFY <password>, RECOVER/RELEASE/GHOST, CERT ADD <fingerprint> for cert auth.\n"
    "- HostServ: REQUEST <vhost>, ON/OFF to toggle. ChanServ: /msg ChanServ HELP REGISTER (don’t guess syntax).\n"
    "\n"
    "Client snippets:\n"
    "- WeeChat: /server add newnet irc.newnet.net/6697 -tls ; set SASL via /set irc.server.newnet.sasl_mechanism plain and /secure for the password; "
    "for CertFP: set tls_cert and sasl_mechanism external; /connect newnet.\n"
    "- HexChat: Network → Add → Server: irc.newnet.net/+6697 ; enable SASL (PLAIN) or CertFP (per-network PEM).\n"
    "- mIRC (Windows): /server irc.newnet.net +6697 ; SASL/EXTERNAL available via /server switches; use client cert if doing EXTERNAL.\n"
    "\n"
    "Safety & policy:\n"
    "- Encourage TLS + SASL or CertFP; avoid advising plain 6667 unless explicitly troubleshooting. "
    "If a request is unsafe or policy-violating, refuse briefly with a rationale.\n"
)

@dataclass
class Cooldown:
    last_ts: float = 0.0
    seconds: int = settings.BOT_COOLDOWN_SECONDS
    def ready(self) -> bool: return (time.time() - self.last_ts) >= self.seconds
    def touch(self) -> None: self.last_ts = time.time()

@dataclass
class OutQueue:
    writer: asyncio.StreamWriter
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    async def sender(self):
        while True:
            msg = await self.queue.get()
            try:
                self.writer.write((msg + "\r\n").encode("utf-8", "replace"))
                await self.writer.drain()
            except Exception as e:
                log.error("send_error", error=str(e))
            await asyncio.sleep(SEND_RATE_PER_SEC)
    async def put(self, line: str): await self.queue.put(line[:IRC_MAX_LINE])

def irc_safe_chunks(text: str, target: str, nick: str, limit: int = IRC_MAX_LINE - PRIVMSG_PREFIX_OVERHEAD) -> List[str]:
    chunks: List[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining); break
        cut = remaining.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if len(chunks) > 1:
        for i in range(len(chunks)):
            chunks[i] = f"[{i+1}/{len(chunks)}] " + chunks[i]
    return chunks

# -------- Conversation memory --------
@dataclass
class Conversation:
    messages: Deque[Dict] = field(default_factory=deque)  # list of OpenAI "message" dicts
    last_seen: float = field(default_factory=time.time)

    def touch(self): self.last_seen = time.time()

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
        self.touch()

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
        self.touch()

    def prune(self, max_turns: int):
        # A "turn" is (user + assistant). Keep the last max_turns turns.
        # Strategy: trim from the left until there are at most 2*max_turns messages.
        while len(self.messages) > 2 * max_turns:
            self.messages.popleft()

    def expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.last_seen) > ttl_seconds

    def build_input(self, system_prompt: str, new_question: str) -> List[Dict]:
        # Compose: [system] + truncated history + current user question
        msgs: List[Dict] = [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]}
        ]
        msgs.extend(list(self.messages))  # already well-formed chunks
        msgs.append({"role": "user", "content": [{"type": "input_text", "text": new_question}]})
        return msgs

class IRCBot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.sendq: Optional[OutQueue] = None
        self.nick = self.s.IRC_NICK
        self.cooldowns: Dict[str, Cooldown] = {}
        self.connected = False
        self.cap_in_progress = False
        self.sasl_in_progress = False
        self.conversations: Dict[str, Conversation] = {}  # key: requesting nick

    async def connect(self):
        self.nick = self.s.IRC_NICK  # try preferred nick every time
        ssl_ctx = None
        if self.s.IRC_USE_TLS:
            ssl_ctx = ssl.create_default_context()
            if not self.s.IRC_VERIFY_TLS:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        log.info("connecting", host=self.s.IRC_HOST, port=self.s.IRC_PORT, tls=self.s.IRC_USE_TLS)

        self.reader, self.writer = await asyncio.open_connection(self.s.IRC_HOST, self.s.IRC_PORT, ssl=ssl_ctx)
        self.sendq = OutQueue(self.writer)
        asyncio.create_task(self.sendq.sender())

        self.cap_in_progress = True
        await self._send_raw("CAP LS 302")
        await self._send_raw(f"NICK {self.nick}")
        await self._send_raw(f"USER {self.s.IRC_USER} 0 * :{self.s.IRC_REALNAME}")

        asyncio.create_task(self.read_loop())

    async def read_loop(self):
        try:
            while not self.reader.at_eof():
                raw = await self.reader.readline()
                if not raw: break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                await self.handle_line(line)
        except Exception as e:
            log.error("read_loop_error", error=str(e))
        finally:
            self.connected = False
            log.warning("disconnected")
            await asyncio.sleep(3)
            await self.reconnect_with_backoff()

    async def reconnect_with_backoff(self, start: float = 1.5, cap: float = 60.0):
        delay = start
        while True:
            try:
                await self.connect()
                return
            except Exception as e:
                log.error("reconnect_failed", error=str(e), retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(cap, delay * 1.8)

    async def handle_line(self, line: str):
        log.debug("irc_rx", line=line)
        if line.startswith("PING"):
            await self._send_raw("PONG " + line.split(" ", 1)[1]); return

        prefix = ""
        if line.startswith(":"):
            prefix, line = line[1:].split(" ", 1)
        if " :" in line:
            args_str, trailing = line.split(" :", 1)
            args = args_str.split(); args.append(trailing)
        else:
            args = line.split()
        if not args: return

        cmd, params = args[0], args[1:]

        if cmd == "CAP":
            await self._handle_cap(prefix, params); return

        if cmd == "AUTHENTICATE":
            if self.sasl_in_progress and params and params[0] == "+":
                b = f"{self.s.IRC_SASL_USERNAME}\0{self.s.IRC_SASL_USERNAME}\0{self.s.IRC_SASL_PASSWORD or ''}"
                await self._send_raw("AUTHENTICATE " + base64.b64encode(b.encode()).decode())
            return

        if cmd in {"900", "903"}:
            if self.cap_in_progress:
                await self._send_raw("CAP END"); self.cap_in_progress = False
            self.sasl_in_progress = False
            return

        if cmd in {"904", "905", "906", "907"}:
            await self._send_raw("CAP END")
            self.cap_in_progress = False
            self.sasl_in_progress = False
            return

        if cmd == "001":
            self.connected = True
            if self.nick != self.s.IRC_NICK and self.s.IRC_NICKSERV_PASS:
                await self.privmsg("NickServ", f"REGAIN {self.s.IRC_NICK} {self.s.IRC_NICKSERV_PASS}")
                await asyncio.sleep(1.0)
                await self._send_raw(f"NICK {self.s.IRC_NICK}")
                self.nick = self.s.IRC_NICK
            for ch in self.s.channels:
                await self._send_raw(f"JOIN {ch}")
            if self.s.IRC_NICKSERV_PASS and not self.s.IRC_SASL:
                await self.privmsg("NickServ", f"IDENTIFY {self.s.IRC_NICKSERV_PASS}")
            return

        if cmd == "433":  # nick in use
            if self.s.IRC_NICKSERV_PASS:
                await self.privmsg("NickServ", f"GHOST {self.s.IRC_NICK} {self.s.IRC_NICKSERV_PASS}")
                await asyncio.sleep(1.0)
                await self._send_raw(f"NICK {self.s.IRC_NICK}")
                self.nick = self.s.IRC_NICK
            else:
                self.nick = self.nick + "_"
                await self._send_raw(f"NICK {self.nick}")
            return

        if cmd == "PRIVMSG" and len(params) >= 2:
            target, message = params[0], params[1]
            nick = prefix.split("!", 1)[0]
            await self._handle_privmsg(nick, target, message)
            return

    async def _handle_cap(self, prefix: str, params: List[str]):
        target = params[0] if len(params) >= 1 else "*"
        subcmd = params[1].upper() if len(params) >= 2 else ""
        rest   = params[2] if len(params) >= 3 else ""
        if subcmd == "LS":
            if self.s.IRC_SASL:
                await self._send_raw("CAP REQ :sasl")
            else:
                await self._send_raw("CAP END"); self.cap_in_progress = False
        elif subcmd == "ACK":
            if "sasl" in rest:
                self.sasl_in_progress = True
                await self._send_raw("AUTHENTICATE PLAIN")
            else:
                await self._send_raw("CAP END"); self.cap_in_progress = False
        elif subcmd == "NAK":
            await self._send_raw("CAP END"); self.cap_in_progress = False

    async def _send_raw(self, line: str):
        if not self.sendq:
            log.error("send_before_connected", line=line); return
        await self.sendq.put(line)

    async def privmsg(self, target: str, text: str):
        for chunk in irc_safe_chunks(text, target, self.nick):
            await self._send_raw(f"PRIVMSG {target} :{chunk}")

    async def _handle_privmsg(self, nick: str, target: str, message: str):
        # Commands
        if message.startswith(settings.BOT_COMMAND_PREFIX):
            parts = message[len(settings.BOT_COMMAND_PREFIX):].strip().split(None, 1)
            if not parts: return
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
            log.debug("cmd_detected", cmd=cmd, rest=rest)
            if cmd == "ask" and rest:
                await self.answer(nick, target, rest); return
            if cmd == "reset":
                self.conversations.pop(nick.lower(), None)
                await self.privmsg(target, f"{nick}: context cleared.")
                return
            if cmd in {"help", "about"}:
                await self.privmsg(target, f"{self.nick}: Use '!ask <question>' or '{self.nick}: question'.")
                return
            if cmd == "version":
                await self.privmsg(target, f"{self.nick}: Python async IRC bot using OpenAI Responses API.")
                return
            return

        # Mentions (colon/comma/space; optional space after punctuation)
        if settings.BOT_RESPOND_TO_MENTIONS:
            m = re.match(rf"(?i)^\s*{re.escape(self.nick)}\s*[:,]?\s*(.+)$", message)
            if m:
                q = m.group(1).strip()
                log.debug("mention_detected", q=q)
                if q:
                    await self.answer(nick, target, q)
                return  # matched mention, done

    # --------- Core Answer with Context ----------
    async def answer(self, nick: str, target: str, question: str):
        asker_key = nick.lower()
        log.debug("answer_start", asker=asker_key, target=target, question=question)

        try:
            # cooldown
            cd = self.cooldowns.setdefault(asker_key, Cooldown())
            if not cd.ready():
                await self.privmsg(target, f"{nick}: Slow down a bit. Try again in a few seconds."); return
            cd.touch()

            # get or init conversation
            conv = self.conversations.get(asker_key)
            if conv is None or conv.expired(CONTEXT_TTL_SECONDS):
                conv = Conversation()
                self.conversations[asker_key] = conv
                log.debug("context_new_or_reset", asker=asker_key)
            conv.prune(CONTEXT_MAX_TURNS)
            conv.add_user(question)

            # moderation (optional, non-blocking on errors) — check only the new question
            if settings.MODERATION_ENABLED:
                try:
                    mod = await aiclient.moderations.create(model=settings.MODERATION_MODEL, input=question)
                    if getattr(mod.results[0], "flagged", False):
                        await self.privmsg(target, f"{nick}: Sorry, your question trips our safety filters.")
                        return
                except Exception as e:
                    log.warning("moderation_failed", error=str(e))

            # Build input with history
            input_messages = conv.build_input(SYSTEM_PROMPT, question)
            log.debug("context_messages_len", count=len(input_messages))

            # stream the model response
            sent_any = False
            full_answer = ""
            buffer = ""
            try:
                stream = await aiclient.responses.create(
                    model=settings.OPENAI_MODEL,
                    input=input_messages,
                    max_output_tokens=settings.OPENAI_MAX_OUTPUT_TOKENS,
                    stream=True,
                )
                start_ts = time.time()
                async for event in stream:
                    et = getattr(event, "type", "")
                    if et == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            buffer += delta
                            full_answer += delta
                            if len(buffer) >= 350 or buffer.endswith(("\n", ". ", "? ", "! ")) or (time.time() - start_ts > 5 and buffer):
                                await self.privmsg(target, f"{nick}: {buffer.strip()}")
                                log.debug("chunk_flush", n_chars=len(buffer))
                                buffer = ""
                                sent_any = True
                                start_ts = time.time()
                    elif et in {"response.completed", "response.output_text.done"}:
                        break
                if buffer.strip():
                    await self.privmsg(target, f"{nick}: {buffer.strip()}")
                    sent_any = True
                    log.debug("chunk_final_flush", n_chars=len(buffer))
            except (RateLimitError, APIStatusError, APIConnectionError, APIError) as e:
                log.error("ai_stream_error", error=str(e))

            # Fallback path if stream yielded nothing
            if not sent_any:
                try:
                    resp = await aiclient.responses.create(
                        model=settings.OPENAI_MODEL,
                        input=input_messages,
                        max_output_tokens=settings.OPENAI_MAX_OUTPUT_TOKENS,
                        stream=False,
                    )
                    final_text = getattr(resp, "output_text", None)
                    if not final_text:
                        try:
                            final_text = "".join(getattr(o, "content", [{"text": ""}])[0].get("text", "") for o in getattr(resp, "output", []))
                        except Exception:
                            final_text = ""
                    if final_text and final_text.strip():
                        await self.privmsg(target, f"{nick}: {final_text.strip()}")
                        full_answer = (full_answer + " " + final_text.strip()).strip()
                        log.debug("fallback_used", chars=len(final_text.strip()))
                    else:
                        await self.privmsg(target, f"{nick}: I heard you, but couldn't generate a reply. Try rephrasing?")
                        log.debug("fallback_empty")
                except (RateLimitError, APIStatusError, APIConnectionError, APIError) as e:
                    log.error("ai_nonstream_error", error=str(e))
                    await self.privmsg(target, f"{nick}: The model is busy or unreachable. Try again shortly.")

            # Store assistant reply in context if we produced anything
            if full_answer.strip():
                conv.add_assistant(full_answer.strip())
                conv.prune(CONTEXT_MAX_TURNS)
            log.debug("answer_done", asker=asker_key, stored_turns=len(conv.messages)//2)
        except Exception as e:
            # Catch-all so we never silently drop replies
            log.error("answer_unhandled_exception", error=str(e))
            try:
                await self.privmsg(target, f"{nick}: Something went wrong handling that. Please try again.")
            except Exception as e2:
                log.error("answer_fallback_send_failed", error=str(e2))

async def main():
    bot = IRCBot(settings)
    await bot.connect()
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
