from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Optional

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - optional dependency
    AsyncOpenAI = None


@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""


class _MessagesAdapter:
    def __init__(self, client: AsyncOpenAI):
        self._client = client

    async def create(
        self,
        model: str,
        max_tokens: int = 1024,
        temperature: Optional[float] = None,
        system: Optional[str] = None,
        messages: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> Any:
        payload = []
        if system:
            payload.append({"role": "system", "content": system})
        for item in messages or []:
            role = item.get("role", "user")
            content = item.get("content", "")
            payload.append({"role": role, "content": content})

        params: dict = {
            "model": model,
            "messages": payload,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            params["temperature"] = temperature
        params.update(kwargs)

        if self._client is None:
            content = ""
        else:
            response = await self._client.chat.completions.create(**params)
            content = response.choices[0].message.content or ""
        return SimpleNamespace(content=[TextBlock(text=content)])


class DeepSeekClient:
    """Thin DeepSeek wrapper built on the OpenAI-compatible API."""

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url) if AsyncOpenAI is not None else None
        self.available = self._client is not None
        self.messages = _MessagesAdapter(self._client)
        self.embeddings = None
