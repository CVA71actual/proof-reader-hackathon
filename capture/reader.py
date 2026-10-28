"""The model call the run controls make, behind one small interface so tests can use a fake reader.

A reader has three parts:
    ready()         None when it can be called, otherwise one plain sentence saying what is missing
    model           the model id it calls; runs/prices.py says whether its cost is known
    call(prompt)    one model call: a Reply (the raw answer, the model's name, the token counts of this call, or
                    the plain sentence for why the call did not succeed)

LiveReader is Muse Spark with the same call pattern as capture/read_table.py (strict JSON, retry once without
response_format on a 400 or 422). The key comes from the environment (MODEL_API_KEY) or ~/.config/proof-reader/env
through read_table.load_env_file, and is never printed, logged or saved. Cost is never decided here: the job
computes it from the token counts with runs/prices.py (a model not in that table has no cost, so no spend).
"""

from __future__ import annotations

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


class LiveReader:
    def _key(self) -> str:
        read_table.load_env_file()
        return os.environ.get("MODEL_API_KEY", "").strip()

    model = read_table.MODEL

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def call(self, prompt: str) -> Reply:
        from openai import OpenAI  # imported here so tests and offline use need no network client

        client = OpenAI(base_url=read_table.BASE_URL, api_key=self._key())
        messages = [{"role": "user", "content": prompt}]
        response_format = "json_object"
        try:
            resp = read_table._call(client, messages, json_mode=True)
        except Exception as e:  # some endpoints reject response_format; retry once without it
            if read_table._status(e) not in (400, 422):
                return Reply(None, read_table.MODEL, error=read_table._plain_error(e))
            response_format = "none"
            try:
                resp = read_table._call(client, messages, json_mode=False)
            except Exception as e2:
                return Reply(None, read_table.MODEL, error=read_table._plain_error(e2))
        raw = resp.choices[0].message.content if resp.choices else None
        usage = getattr(resp, "usage", None)
        extra = {"base_url": read_table.BASE_URL, "requested_model": read_table.MODEL,
                 "response_format": response_format}
        if usage is not None:
            extra["usage"] = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return Reply(raw, getattr(resp, "model", None) or read_table.MODEL,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=extra)
