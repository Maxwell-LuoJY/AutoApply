"""Chat client for the OpenAI-compatible endpoint in .env, pooled over API keys and models.

The endpoint rate-limits hard ("inference exceeds tpm/rpm limit"). Every (key, model) pair is a slot with its own
pacing; a slot that gets a 429 cools down for a while and the next call goes to another key, then to the next model
in `llm.fallback_models` (settings.yaml). The model in .env (LLM_MODEL) is always preferred.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from openai import APIConnectionError, BadRequestError, InternalServerError, NotFoundError, OpenAI, RateLimitError

from . import config

log = logging.getLogger("jobagent")


class _RateLimiter:
    """Spaces request starts at least `interval` seconds apart across all threads (the endpoint 429s on bursts).
    The interval grows after each 429 and creeps back to 1/rps after successes, so a busy key stops being hammered."""

    MAX_INTERVAL = 30.0

    def __init__(self, rps: float):
        self.base = self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def slow_down(self) -> None:
        with self.lock:
            self.interval = min(self.MAX_INTERVAL, self.interval * 1.6)

    def speed_up(self) -> None:
        with self.lock:
            self.interval = max(self.base, self.interval * 0.9)

    def reserve(self) -> float:
        """Book the next slot and return how long the caller must wait for it."""
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.interval
        return slot - now

    def wait(self) -> None:
        time.sleep(max(0.0, self.reserve()))


@dataclass
class _Slot:
    client: OpenAI
    model: str
    key_no: int
    limiter: _RateLimiter
    temperature: float = 0.0
    cool_until: float = 0.0     # time.monotonic() before which this slot is skipped
    strikes: int = 0            # consecutive 429s
    lock: threading.Lock = field(default_factory=threading.Lock)

    def rate_limited(self) -> None:
        with self.lock:
            self.strikes += 1
            self.cool_until = time.monotonic() + min(120.0, 15.0 * 1.6 ** (self.strikes - 1))
        self.limiter.slow_down()

    def succeeded(self) -> None:
        with self.lock:
            self.strikes = 0
        self.limiter.speed_up()


class LLM:
    """Thread-safe. Prefers the .env model on any key; uses the fallback models only while it is rate-limited."""

    PATIENCE = 400   # seconds one call may wait for a free slot before giving up (unattended batches can afford it)
    BUSY = 10.0      # a slot whose pacing would make us wait longer than this goes behind the free ones

    def __init__(self, rps: float = 1.0) -> None:
        keys, base_url, model = config.llm_credentials()
        fallbacks = (config.settings().get("llm") or {}).get("fallback_models") or []
        self.models = list(dict.fromkeys([model, *fallbacks]))
        # No SDK retries: its quick 429 retries only add to the burst; _create rotates slots instead.
        clients = [OpenAI(api_key=k, base_url=base_url, timeout=180, max_retries=0) for k in keys]
        self.slots = [_Slot(c, m, i, _RateLimiter(rps)) for m in self.models for i, c in enumerate(clients)]
        self.model = model
        self.usage: Counter[str] = Counter()   # successful calls per model, for the run summary
        self._turn = 0
        self._turn_lock = threading.Lock()

    @property
    def key_count(self) -> int:
        return len({s.key_no for s in self.slots})

    def json_chat(self, system: str, user: str, max_tokens: int = 6000) -> dict:
        """One JSON-mode completion parsed into a dict. Reasoning tokens count toward max_tokens.
        A reply that isn't JSON (some models return '' when they run out of tokens) is retried on another model."""
        last, avoid = "", set()
        for _ in range(3):
            resp = self._create(system, user, max_tokens, avoid)
            last = resp.choices[0].message.content or ""
            try:
                return json.loads(last)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", last, re.S)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        pass
            log.info("LLM %s 没有返回 JSON（finish_reason=%s），换一个模型再问", resp.model,
                     getattr(resp.choices[0], "finish_reason", "?"))
            avoid.add(resp.model)
            if len(avoid) >= len(self.models):
                avoid.clear()
        raise ValueError(f"LLM 没有返回合法 JSON：{last[:200]!r}")

    def _ordered(self, avoid: set[str] = frozenset()) -> list[_Slot]:
        """Slots not cooling down: free ones before heavily paced ones, then the preferred model, keys taking turns."""
        with self._turn_lock:
            self._turn += 1
            turn = self._turn
        now, keys = time.monotonic(), self.key_count
        return sorted((s for s in self.slots if s.cool_until <= now and s.model not in avoid),
                      key=lambda s: (s.limiter.next_slot - now > self.BUSY, self.models.index(s.model),
                                     (s.key_no + turn) % keys))

    def _create(self, system: str, user: str, max_tokens: int, avoid: set[str] = frozenset()):
        deadline = time.monotonic() + self.PATIENCE
        while True:
            for s in self._ordered(avoid):
                s.limiter.wait()
                try:
                    resp = s.client.chat.completions.create(
                        model=s.model,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        response_format={"type": "json_object"},
                        max_tokens=max_tokens,
                        temperature=s.temperature,
                    )
                except RateLimitError:
                    s.rate_limited()
                    continue
                except (APIConnectionError, InternalServerError):
                    s.cool_until = time.monotonic() + 10  # transient network / server error
                    continue
                except BadRequestError as e:
                    if re.search(r"(?i)temperature", str(e)) and s.temperature != 1.0:
                        s.temperature = 1.0  # some models (kimi-k3) only accept temperature=1
                    else:
                        log.warning("LLM 模型 %s 不接受这种请求，之后不再使用：%s", s.model, str(e)[:160])
                        s.cool_until = math.inf
                    continue
                except NotFoundError as e:
                    log.warning("LLM 模型 %s 不存在，之后不再使用：%s", s.model, str(e)[:160])
                    s.cool_until = math.inf
                    continue
                s.succeeded()
                self.usage[s.model] += 1
                resp.model = s.model  # the slot's name, whatever alias the endpoint echoes back
                return resp
            if time.monotonic() > deadline:
                raise RuntimeError("所有 API key 都被限流，稍后再试")
            soonest = min(s.cool_until for s in self.slots)
            if soonest == math.inf:
                raise RuntimeError("没有可用的 LLM 模型（都被判定为不可用）")
            time.sleep(min(30.0, max(0.5, soonest - time.monotonic())))
