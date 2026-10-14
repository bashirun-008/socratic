"""Provider layer: the rest of the app never knows which AI company is behind the tutor.

Internal message format (provider-neutral):
  {"role": "user" | "assistant", "text": str, "image": (bytes, mime_type) | None}
"""
import base64
import os

from prompt import SYSTEM_PROMPT


def _gemini_reply(history: list[dict]) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    contents = []
    for m in history:
        parts = []
        if m.get("image"):
            data, mime = m["image"]
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        parts.append(types.Part.from_text(text=m["text"]))
        role = "user" if m["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=parts))

    resp = client.models.generate_content(
        model=os.environ["GEMINI_MODEL"],
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=800,
        ),
    )
    return resp.text


def _groq_reply(history: list[dict]) -> str:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        if m["role"] == "assistant":
            messages.append({"role": "assistant", "content": m["text"]})
            continue
        content = []
        if m.get("image"):
            data, mime = m["image"]
            b64 = base64.b64encode(data).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        content.append({"type": "text", "text": m["text"]})
        messages.append({"role": "user", "content": content})

    resp = client.chat.completions.create(
        model=os.environ["GROQ_VISION_MODEL"],
        messages=messages,
        max_tokens=800,
        temperature=0.4,
    )
    return resp.choices[0].message.content


def tutor_reply(history: list[dict], provider: str | None = None) -> str:
    provider = (provider or os.getenv("PROVIDER", "gemini")).lower()
    if provider == "gemini":
        return _gemini_reply(history)
    if provider == "groq":
        return _groq_reply(history)
    raise ValueError(f"Unknown provider: {provider}")


def transcribe(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Speech-to-text via Groq Whisper (free tier). Used for the voice note."""
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    result = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
    )
    return result.text


def llm_json(system: str, user: str, provider: str | None = None, max_tokens: int = 1800) -> str:
    """Text-only call that should return JSON (used to write quiz questions)."""
    provider = (provider or os.getenv("PROVIDER", "gemini")).lower()
    if provider == "groq":
        from groq import Groq

        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        model = os.getenv("GROQ_TEXT_MODEL") or os.environ["GROQ_VISION_MODEL"]
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.4,
        )
        return resp.choices[0].message.content
    if provider == "gemini":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        resp = client.models.generate_content(
            model=os.environ["GEMINI_MODEL"],
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                max_output_tokens=max_tokens,
            ),
        )
        return resp.text
    raise ValueError(f"Unknown provider: {provider}")
