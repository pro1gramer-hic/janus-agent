"""HTTP helper shared by the providers: JSON POST with retries on 429 and 5xx."""

import time

import httpx

from janus_agent.llm.base import ProviderError

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class HTTPStatusError(ProviderError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def post_json(url: str, payload: dict, headers: dict, timeout: float, retries: int = 3) -> dict:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise ProviderError(f"cannot reach {url}: {exc}") from exc
        else:
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(f"{url} did not return JSON") from exc
            if response.status_code not in RETRY_STATUS or attempt == retries:
                raise HTTPStatusError(response.status_code, response.text)
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                delay = max(delay, float(retry_after))
        time.sleep(delay)
        delay *= 2
    raise ProviderError("unreachable")
