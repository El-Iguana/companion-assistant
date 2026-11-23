import httpx
from services.muninn.text import strip_reasoning
import json
import re
import time
from typing import AsyncGenerator, Optional


# ---------------------------------------------------------------------------
# Think-block stripper
# Qwen3 / reasoning models emit <think>...</think> before the real response.
# We strip these in a streaming-safe way so the UI never sees them.
# ---------------------------------------------------------------------------
_OPEN  = "<think>"
_CLOSE = "</think>"

def _strip_think(buf: str, in_think: bool) -> tuple[str, str, bool]:
    """
    Process one chunk of accumulated text.
    Returns (text_to_emit, remaining_buffer, updated_in_think).
    """
    output = ""
    while buf:
        if in_think:
            end = buf.find(_CLOSE)
            if end != -1:
                # Found closing tag — discard up to and including it, strip leading newlines
                buf = buf[end + len(_CLOSE):].lstrip("\n")
                in_think = False
            else:
                # Closing tag may be split across future tokens — keep a tail that
                # could still be a partial match; discard everything before it.
                for i in range(len(_CLOSE) - 1, 0, -1):
                    if buf.endswith(_CLOSE[:i]):
                        buf = buf[-i:]          # keep the partial candidate
                        return output, buf, in_think
                buf = ""                        # nothing to keep, still inside block
                return output, buf, in_think
        else:
            start = buf.find(_OPEN)
            if start != -1:
                output += buf[:start]           # emit text before the tag
                buf = buf[start + len(_OPEN):]
                in_think = True
            else:
                # Opening tag may be split across future tokens
                for i in range(len(_OPEN) - 1, 0, -1):
                    if buf.endswith(_OPEN[:i]):
                        output += buf[:-i]      # emit text before partial candidate
                        buf = buf[-i:]
                        return output, buf, in_think
                output += buf
                buf = ""
                return output, buf, in_think
    return output, buf, in_think


# ---------------------------------------------------------------------------
# Inline [expr:label] tag parser
#
# A sibling of _strip_think, not a reuse of it. Two differences drive the split:
#   * the body is variable-length, so we scan to a delimiter instead of matching
#     a second fixed literal;
#   * the body is *emitted* as an event rather than discarded.
# What does carry over is the partial-tail trick: a tag can be split across
# tokens, so we keep any suffix that could still become one.
#
# Yields text and events interleaved, in order, so the caller can preserve the
# tag's position in the stream — the frontend needs it to fire the expression at
# the right point in the reply rather than at the end of the chunk.
# ---------------------------------------------------------------------------
_EXPR_OPEN = "[expr:"
_EXPR_CLOSE = "]"
# A real label is one short word. If we pass this without finding a close bracket
# it was never a tag, so give the text back verbatim instead of eating the reply.
_EXPR_MAX_LABEL = 32


class ExprEvent(str):
    """An expression beat parsed out of the stream.

    Subclasses str so a caller that only cares about text stays correct, while
    `isinstance(item, ExprEvent)` distinguishes an event from a token.
    """
    __slots__ = ()


def _strip_expr(buf: str, in_expr: bool) -> tuple[list, str, bool]:
    """Process one chunk of accumulated (already think-stripped) text.

    Returns (items, remaining_buffer, updated_in_expr) where items is an ordered
    mix of plain `str` text runs and `ExprEvent` labels.
    """
    items: list = []

    def emit(text: str) -> None:
        if not text:
            return
        if items and type(items[-1]) is str:
            items[-1] += text          # coalesce adjacent text runs
        else:
            items.append(text)

    while buf:
        if in_expr:
            end = buf.find(_EXPR_CLOSE)
            if end != -1:
                label = buf[:end].strip()
                buf = buf[end + len(_EXPR_CLOSE):]
                in_expr = False
                if label:
                    items.append(ExprEvent(label))
            elif len(buf) > _EXPR_MAX_LABEL:
                emit(_EXPR_OPEN + buf)  # never a tag — restore it as literal text
                buf = ""
                in_expr = False
            else:
                # Label may still be completed by a later token.
                return items, buf, in_expr
        else:
            start = buf.find(_EXPR_OPEN)
            if start != -1:
                emit(buf[:start])
                buf = buf[start + len(_EXPR_OPEN):]
                in_expr = True
            else:
                # Opening tag may be split across future tokens.
                for i in range(len(_EXPR_OPEN) - 1, 0, -1):
                    if buf.endswith(_EXPR_OPEN[:i]):
                        emit(buf[:-i])
                        return items, buf[-i:], in_expr
                emit(buf)
                return items, "", in_expr
    return items, buf, in_expr


def _flush_expr(buf: str, in_expr: bool) -> list:
    """End of stream: give back any half-parsed tag as the literal text it was."""
    if not buf:
        return []
    return [(_EXPR_OPEN + buf) if in_expr else buf]


class KoboldClient:
    def __init__(self, base_url: str = "http://localhost:5001"):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        return self._client

    async def check_status(self) -> dict:
        try:
            resp = await self.client.get(f"{self.base_url}/api/v1/info/version", timeout=5.0)
            data = resp.json()
            model_resp = await self.client.get(f"{self.base_url}/api/v1/model", timeout=5.0)
            model = model_resp.json().get("result", "Unknown")
            return {"online": True, "version": data.get("result", "unknown"), "model": model,
                    "context": await self.context_limit()}
        except Exception as e:
            return {"online": False, "error": str(e)}

    # The backend's real context window. Asked, not assumed: swapping the model or
    # changing --contextsize changes it, and an app budgeting against a stale number
    # gets its prompt cut from the front. Cached briefly because every turn asks;
    # a restart with a new size is picked up within CONTEXT_TTL_S.
    CONTEXT_TTL_S = 30.0
    CONTEXT_RETRY_S = 10.0     # a failed probe is not retried sooner than this

    async def context_limit(self) -> Optional[int]:
        """Tokens the loaded model accepts, or the last known value, or None if never seen."""
        now = time.monotonic()
        value, at, ok = getattr(self, "_ctx_cache", (None, -1e9, False))
        if now - at < (self.CONTEXT_TTL_S if ok else self.CONTEXT_RETRY_S):
            return value
        try:
            resp = await self.client.get(
                f"{self.base_url}/api/extra/true_max_context_length", timeout=3.0)
            resp.raise_for_status()
            fresh = int(resp.json().get("value") or 0)
        except Exception:
            fresh = 0
        if fresh > 0:
            self._ctx_cache = (fresh, now, True)
            return fresh
        # Down or unreadable: keep the last good number, and don't ask again on every turn.
        self._ctx_cache = (value, now, False)
        return value

    async def generate(self, prompt: str, params: dict) -> str:
        return (await self.generate_full(prompt, params))[0]

    async def generate_full(self, prompt: str, params: dict) -> tuple[str, bool]:
        """(text, truncated). `truncated` = the reply used its whole `max_length`, i.e. it
        was cut off rather than finished. Read from `completion_tokens`, not
        `finish_reason`: KoboldCPP reports "length" even for a reply a stop sequence
        ended after 3 of 40 tokens."""
        payload = {"prompt": prompt, **params}
        resp = await self.client.post(
            f"{self.base_url}/api/v1/generate",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["results"][0]
        text = result["text"]
        cap, used = params.get("max_length"), result.get("completion_tokens")
        truncated = bool(cap and isinstance(used, int) and used >= cap)
        # Strip any think blocks from non-streaming output
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip(), truncated

    async def generate_answer(self, prompt: str, params: dict) -> str:
        """Answer `prompt` directly, bypassing this model's runaway <think>: wrap it in a
        ChatML turn with a PREFILLED empty <think></think> so generation continues at the
        answer (the reliable way to disable thinking for small classifier/summary calls).
        Raw /api/v1/generate → use max_length in params.

        Returns the answer only. A model that doesn't follow the prefilled-think convention
        writes its reasoning anyway and ends it with a stray `</think>`/`</thought>`, or ends
        the answer with another template's tag (`</assistant>`); all of that used to be stored
        as memory. `muninn.text.strip_reasoning` keeps only the answer."""
        return (await self.generate_answer_full(prompt, params))[0]

    async def generate_answer_full(self, prompt: str, params: dict) -> tuple[str, bool]:
        """generate_answer, plus whether the answer hit its `max_length` (see generate_full)."""
        chatml = (f"<|im_start|>user\n{prompt}<|im_end|>\n"
                  f"<|im_start|>assistant\n<think>\n\n</think>\n\n")
        text, truncated = await self.generate_full(
            chatml, {"stop_sequence": ["<|im_end|>"], **params})
        return strip_reasoning(text), truncated

    async def chat_generate_stream(
        self, messages: list[dict], params: dict
    ) -> AsyncGenerator[str, None]:
        """Stream via KoboldCPP's OpenAI-compatible /v1/chat/completions endpoint.

        If the model hits the token limit mid-sentence (finish_reason == "length"),
        transparently continues generating until a natural sentence boundary is
        reached, with one extra round that resumes the same reply (assistant prefill).
        The caller sees one seamless stream.
        """
        buf = ""
        in_think = False
        ebuf = ""            # expr-parser buffer: text that may end mid-tag
        in_expr = False
        full_text = ""
        current_messages = list(messages)

        for _round in range(2):  # initial + up to 1 continuation (avoids rambling/repetition)
            finish_reason = None
            round_text = ""
            payload = {"messages": current_messages, "stream": True, **params}

            try:
                async with self.client.stream(
                    "POST",
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        if not raw:
                            continue
                        try:
                            parsed = json.loads(raw)
                            choices = parsed.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                round_text += token
                                buf += token
                                out, buf, in_think = _strip_think(buf, in_think)
                                if out:
                                    items, ebuf, in_expr = _strip_expr(ebuf + out, in_expr)
                                    for it in items:
                                        yield it
                            fr = choices[0].get("finish_reason")
                            if fr and fr not in (None, "null", ""):
                                finish_reason = fr
                                break
                        except json.JSONDecodeError:
                            pass
            except Exception:
                # Fallback: non-streaming
                try:
                    ns_payload = {**payload, "stream": False}
                    resp = await self.client.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=ns_payload,
                    )
                    resp.raise_for_status()
                    text = resp.json()["choices"][0]["message"]["content"]
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                    round_text = text
                    buf += text
                    out, buf, in_think = _strip_think(buf, in_think)
                    if out:
                        items, ebuf, in_expr = _strip_expr(ebuf + out, in_expr)
                        for it in items:
                            yield it
                    finish_reason = "stop"
                except Exception as e:
                    raise RuntimeError(f"Chat generation failed: {e}") from e

            full_text += round_text

            # Natural stop — flush buffer and we're done
            if finish_reason != "length":
                if buf and not in_think:
                    items, ebuf, in_expr = _strip_expr(ebuf + buf, in_expr)
                    for it in items:
                        yield it
                    buf = ""
                break

            # Hit token limit — check if we ended on a sentence boundary
            stripped = full_text.strip()
            if re.search(r'[.!?]["\u201d\u2019]?\s*$', stripped):
                if buf and not in_think:
                    items, ebuf, in_expr = _strip_expr(ebuf + buf, in_expr)
                    for it in items:
                        yield it
                    buf = ""
                break

            # Mid-sentence: resume the SAME reply. A trailing assistant message is
            # treated by KoboldCPP as a prefill and generation picks up mid-sentence
            # (verified live: "...and her captain" -> " is a fearsome woman named
            # Sarah."). This replaced a fake user turn saying "Continue.", which the
            # model read as a new turn and answered by recapping the scene — the
            # recap was then glued onto the reply. Nothing may follow the prefill,
            # so the trailing system note stays where it already is, just before it.
            # The extra round only needs to finish a sentence, so it gets a small cap.
            visible = re.sub(r"<think>.*?(</think>|$)", "", full_text, flags=re.DOTALL).lstrip()
            if not visible:
                # The whole round went on <think> and nothing was said. Start the
                # reply over with an empty think block prefilled, which skips the
                # reasoning, at the full token budget.
                buf, in_think = "", False
                current_messages = list(messages) + [
                    {"role": "assistant", "content": "<think>\n\n</think>\n\n"}]
                continue
            current_messages = list(messages) + [{"role": "assistant", "content": visible}]
            params = {**params, "max_tokens": min(int(params.get("max_tokens") or 80), 80)}

        else:
            # Exhausted all continuation rounds — flush whatever we have
            if buf and not in_think:
                items, ebuf, in_expr = _strip_expr(ebuf + buf, in_expr)
                for it in items:
                    yield it

        # A tag left half-parsed at end of stream was never a tag — emit it as text.
        for it in _flush_expr(ebuf, in_expr):
            yield it

    async def generate_stream(self, prompt: str, params: dict) -> AsyncGenerator[str, None]:
        payload = {"prompt": prompt, **params}
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/api/extra/generate/stream",
                json=payload,
            ) as resp:
                resp.raise_for_status()
                buf = ""
                in_think = False
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw in ("", "[DONE]"):
                        break
                    try:
                        parsed = json.loads(raw)
                        token = parsed.get("token", "")
                        if token:
                            buf += token
                            out, buf, in_think = _strip_think(buf, in_think)
                            if out:
                                yield out
                        if parsed.get("done") or parsed.get("finish_reason"):
                            break
                    except json.JSONDecodeError:
                        pass
                # Flush remaining buffer (only if we're not still inside a block)
                if buf and not in_think:
                    yield buf
        except Exception:
            # Fallback: non-streaming
            text = await self.generate(prompt, params)
            yield text

    async def text_to_image(self, prompt: str, params: dict) -> Optional[str]:
        payload = {"prompt": prompt, **params}
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/extra/txt2img",
                json=payload,
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            images = data.get("images", [])
            return images[0] if images else None
        except Exception as e:
            raise RuntimeError(f"Image generation failed: {e}") from e

    async def abort(self, genkey: str) -> None:
        """Stop the generation started with this `genkey` (KoboldCPP /api/extra/abort).
        The cancelled call returns at once with partial text that still reports
        finish_reason "length", so the caller must discard it. Never raises."""
        try:
            await self.client.post(f"{self.base_url}/api/extra/abort", json={"genkey": genkey},
                                   timeout=5.0)
        except Exception:
            pass

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
