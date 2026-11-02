"""The model call the run controls make, behind one small interface so tests can use a fake reader.

A reader has three parts:
    ready()         None when it can be called, otherwise one plain sentence saying what is missing
    model           the model id it calls; runs/prices.py says whether its cost is known
    call(prompt, image=None)
                    one model call: a Reply (the raw answer, the model's name, the token counts of this call, or
                    the plain sentence for why the call did not succeed). `image`: PNG bytes of a page or a region,
                    sent beside the prompt as a data-URI image_url (the form Muse Spark Open and Muse Wonder accepted)

LiveReader is Muse Spark with the same call pattern as capture/read_table.py (strict JSON). A 400 or 422 is never
retried automatically (PIVOT RED 3): the failed request is returned with its status and may_have_been_billed, and
any further request is a new attempt through the job's gate. The key comes from the environment (MODEL_API_KEY) or ~/.config/proof-reader/env
through read_table.load_env_file, and is never printed, logged or saved. Cost is never decided here: the job
computes it from the token counts with runs/prices.py (a model not in that table has no cost, so no spend).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from capture import http, read_table

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
    tool_calls: list | None = None  # OpenAI-shape tool_calls of a chat() answer that asked for the tools
    timed_out: bool = False        # the service did not answer (after the one retry); error holds the plain reason


def messages_for(prompt: str, image: bytes | None = None) -> list[dict]:
    """The chat messages for a prompt, with an image (PNG bytes) beside it when there is one."""
    if image is None:
        return [{"role": "user", "content": prompt}]
    uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    return [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": uri}}]}]


def retried(extra: dict | None) -> dict | None:
    """extra with retried_after_timeout set when the call just made was answered only on a retry that followed a
    possibly-billed timeout (capture/http.py take_billed); extra unchanged otherwise."""
    if http.take_billed():
        return {**(extra or {}), "retried_after_timeout": True}
    return extra


def timeout_reply(name: str, model: str, images: int | None = None, extra: dict | None = None,
                  exc: BaseException | None = None) -> Reply:
    """The Reply of a call whose service did not answer, even after the one retry (capture/http.py). extra carries
    may_have_been_billed: whether ANY attempt may have been billed (False only when every attempt failed in the connect phase);
    True when the failure is not known."""
    earlier = http.take_billed()       # an earlier attempt of this call may have been sent: any such attempt is billable
    flag = True if exc is None else (earlier or http.may_have_been_billed(exc))
    return Reply(None, model, error=http.did_not_answer(name), timed_out=True, images=images,
                 extra={**(extra or {}), "may_have_been_billed": flag})


def create(client, model: str, messages: list[dict], json_mode: bool):
    """One chat call, capped at http.MAX_COMPLETION_TOKENS (roomy: these models reason before they answer)."""
    kwargs = {"model": model, "messages": messages, "temperature": 0,
              "max_tokens": http.MAX_COMPLETION_TOKENS}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return http.call_with_retry(lambda: client.chat.completions.create(**kwargs))


class LiveReader:
    """Muse Spark on the Meta Model API. WonderReader (capture/wonder.py) is the same call to another model."""

    model = read_table.MODEL
    name = "Muse Spark"

    def _key(self) -> str:
        read_table.load_env_file()
        return os.environ.get("MODEL_API_KEY", "").strip()

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def call(self, prompt: str, image: bytes | None = None) -> Reply:
        client = http.openai_client(read_table.BASE_URL, self._key())
        messages = messages_for(prompt, image)
        response_format = "json_object"
        http.take_billed()
        try:
            resp = create(client, self.model, messages, json_mode=True)
        except Exception as e:  # no automatic retry on 400/422: one request, one auditable attempt
            if http.is_silent(e):
                return timeout_reply(self.name, self.model, exc=e)
            return Reply(None, self.model, error=read_table._plain_error(e),
                         extra={"route": "meta", **read_table.failure_billing(e)})
        raw = resp.choices[0].message.content if resp.choices else None
        usage = getattr(resp, "usage", None)
        extra = {"base_url": read_table.BASE_URL, "requested_model": self.model,
                 "response_format": response_format, "input": "image" if image is not None else "words"}
        if usage is not None:
            extra["usage"] = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return Reply(raw, getattr(resp, "model", None) or self.model,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=retried(extra))

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> Reply:
        """One call of a conversation (the tool loop in server/jobs.py). With `tools` the OpenAI "tools" parameter is
        sent; if api.meta.ai rejects it (400 or 422) the Reply has an error and extra["tools_rejected"] is True, and the
        caller goes on with the JSON tool request (capture/reader_tools.py). Never raises."""
        client = http.openai_client(read_table.BASE_URL, self._key())
        kwargs = {"model": self.model, "messages": messages, "temperature": 0,
              "max_tokens": http.MAX_COMPLETION_TOKENS}
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        http.take_billed()
        try:
            resp = http.call_with_retry(lambda: client.chat.completions.create(**kwargs))
        except Exception as e:
            if http.is_silent(e):
                return timeout_reply(self.name, self.model, exc=e)
            rejected = bool(tools) and read_table._status(e) in (400, 422)
            # The rejected request is itself a possibly-billed attempt; the JSON tool
            # request that may follow is a new call through the job's gate.
            return Reply(None, self.model, error=read_table._plain_error(e),
                         extra={"tools_rejected": rejected, "status": read_table._status(e), "route": "meta",
                                **read_table.failure_billing(e)})
        msg = resp.choices[0].message if resp.choices else None
        calls = None
        if msg is not None and getattr(msg, "tool_calls", None):
            calls = [{"id": c.id, "type": "function",
                      "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in msg.tool_calls]
        usage = getattr(resp, "usage", None)
        extra = {"base_url": read_table.BASE_URL, "requested_model": self.model, "input": "words",
                 "response_format": "json_object" if json_mode else "none", "tools_sent": bool(tools)}
        return Reply(getattr(msg, "content", None) if msg is not None else None, getattr(resp, "model", None) or self.model,
                     prompt_tokens=getattr(usage, "prompt_tokens", None),
                     completion_tokens=getattr(usage, "completion_tokens", None), extra=retried(extra), tool_calls=calls)
