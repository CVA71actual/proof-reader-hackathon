"""The model call the run controls make, behind one small interface so tests can use a fake reader.

A reader has three parts:
    ready()         None when it can be called, otherwise one plain sentence saying what is missing
    model           the model id it calls; runs/prices.py says whether its cost is known
    call(prompt, image=None)
                    one model call: a Reply (the raw answer, the model's name, the token counts of this call, or
                    the plain sentence for why the call did not succeed). `image`: PNG bytes of a page or a region,
                    sent beside the prompt as a data-URI image_url (the form Muse Spark Open and Muse Wonder accepted)

LiveReader is Muse Spark with the same call pattern as capture/read_table.py (strict JSON, retry once without
response_format on a 400 or 422). The key comes from the environment (MODEL_API_KEY) or ~/.config/proof-reader/env
through read_table.load_env_file, and is never printed, logged or saved. Cost is never decided here: the job
computes it from the token counts with runs/prices.py (a model not in that table has no cost, so no spend).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from capture import read_table

NO_KEY = ("No model key was found, so nothing was read. Add MODEL_API_KEY to ~/.config/proof-reader/env "
          "and resume.")


@dataclass
class Reply:
    raw: str | None
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    extra: dict | None = None
    images: int | None = None      # images processed, for a model priced per image (SAM 3.1)


def messages_for(prompt: str, image: bytes | None = None) -> list[dict]:
    """The chat messages for a prompt, with an image (PNG bytes) beside it when there is one."""
    if image is None:
        return [{"role": "user", "content": prompt}]
    uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    return [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": uri}}]}]


def create(client, model: str, messages: list[dict], json_mode: bool):
    """One chat call. No token cap: these models reason before they answer and a small cap leaves the answer empty."""
    kwargs = {"model": model, "messages": messages, "temperature": 0}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs)


class LiveReader:
    """Muse Spark on the Meta Model API. WonderReader (capture/wonder.py) is the same call to another model."""

    model = read_table.MODEL

    def _key(self) -> str:
        read_table.load_env_file()
        return os.environ.get("MODEL_API_KEY", "").strip()

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def call(self, prompt: str, image: bytes | None = None) -> Reply:
        from openai import OpenAI  # imported here so tests and offline use need no network client

        client = OpenAI(base_url=read_table.BASE_URL, api_key=self._key())
        messages = messages_for(prompt, image)
        response_format = "json_object"
        try:
            resp = create(client, self.model, messages, json_mode=True)
        except Exception as e:  # some endpoints reject response_format; retry once without it
            if read_table._status(e) not in (400, 422):
                return Reply(None, self.model, error=read_table._plain_error(e))
            response_format = "none"
            try:
                resp = create(client, self.model, messages, json_mode=False)
            except Exception as e2:
                return Reply(None, self.model, error=read_table._plain_error(e2))
        raw = resp.choices[0].message.content if resp.choices else None
        usage = getattr(resp, "usage", None)
        extra = {"base_url": read_table.BASE_URL, "requested_model": self.model,
                 "response_format": response_format, "input": "image" if image is not None else "words"}
        if usage is not None:
            extra["usage"] = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return Reply(raw, getattr(resp, "model", None) or self.model,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=extra)
