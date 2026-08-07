import httpx
import json
import re
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
            return {"online": True, "version": data.get("result", "unknown"), "model": model}
        except Exception as e:
            return {"online": False, "error": str(e)}

    async def generate(self, prompt: str, params: dict) -> str:
        payload = {"prompt": prompt, **params}
        resp = await self.client.post(
            f"{self.base_url}/api/v1/generate",
            json=payload,
        )
        resp.raise_for_status()
        text = resp.json()["results"][0]["text"]
        # Strip any think blocks from non-streaming output
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    async def generate_answer(self, prompt: str, params: dict) -> str:
        """Answer `prompt` directly, bypassing this model's runaway <think>: wrap it in a
        ChatML turn with a PREFILLED empty <think></think> so generation continues at the
        answer (the reliable way to disable thinking for small classifier/summary calls).
        Raw /api/v1/generate → use max_length in params. Returns the answer (think stripped)."""
        chatml = (f"<|im_start|>user\n{prompt}<|im_end|>\n"
                  f"<|im_start|>assistant\n<think>\n\n</think>\n\n")
        return await self.generate(chatml, {"stop_sequence": ["<|im_end|>"], **params})

    async def chat_generate_stream(
        self, messages: list[dict], params: dict
    ) -> AsyncGenerator[str, None]:
        """Stream via KoboldCPP's OpenAI-compatible /v1/chat/completions endpoint.

        If the model hits the token limit mid-sentence (finish_reason == "length"),
        transparently continues generating until a natural sentence boundary is
        reached, up to 3 extra rounds. The caller sees one seamless stream.
        """
        buf = ""
        in_think = False
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
                                    yield out
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
                        yield out
                    finish_reason = "stop"
                except Exception as e:
                    raise RuntimeError(f"Chat generation failed: {e}") from e

            full_text += round_text

            # Natural stop — flush buffer and we're done
            if finish_reason != "length":
                if buf and not in_think:
                    yield buf
                    buf = ""
                break

            # Hit token limit — check if we ended on a sentence boundary
            stripped = full_text.strip()
            if re.search(r'[.!?]["\u201d\u2019]?\s*$', stripped):
                if buf and not in_think:
                    yield buf
                    buf = ""
                break

            # Mid-sentence: continue from where we left off
            current_messages = list(messages) + [
                {"role": "assistant", "content": full_text},
                {"role": "user", "content": "Continue."},
            ]

        else:
            # Exhausted all continuation rounds — flush whatever we have
            if buf and not in_think:
                yield buf

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

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
