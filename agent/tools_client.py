from __future__ import annotations

from typing import Any

import httpx


class ToolsAPIError(RuntimeError):
    pass


class ToolsClient:
    def __init__(
        self,
        base_url: str,
        *,
        session_id: str,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout
        self._client = client

    async def call(self, endpoint: str, **payload: Any) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            trust_env=False,
        )
        try:
            response = await client.post(
                f"/{endpoint}",
                json=payload,
                headers={"x-session-id": self.session_id},
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ToolsAPIError("The local tools service returned an invalid response.")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolsAPIError(
                f"The {endpoint.replace('_', ' ')} service is temporarily unavailable."
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
