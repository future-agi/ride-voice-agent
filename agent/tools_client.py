from __future__ import annotations

import json
import os
from pathlib import Path
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
        self._trace_path: Path | None = None

    @property
    def harness_mode(self) -> bool:
        """Whether this client is running under an isolated harness scenario.

        Harness mode is explicit.  A trace destination was the original signal used by the
        voice runner, while ``HARNESS_MODE`` lets a containerised runner enable the same
        behaviour without requiring a shared filesystem mount.
        """
        return os.environ.get("HARNESS_MODE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        } or bool(os.environ.get("HARNESS_TOOL_TRACE", "").strip())

    def enable_trace(self, destination: str | None = None) -> None:
        """Trace API-boundary calls, including deterministic calls outside LLM tools."""
        value = destination or os.environ.get("HARNESS_TOOL_TRACE", "")
        if value.strip():
            self._trace_path = Path(value)
            self._trace_path.parent.mkdir(parents=True, exist_ok=True)

    def record_local(
        self, name: str, arguments: dict[str, Any], output: dict[str, Any]
    ) -> None:
        """Record a state-only tool that does not cross the HTTP boundary."""
        self._record(name, arguments, output, is_error=False)

    def _record(
        self,
        endpoint: str,
        payload: dict[str, Any],
        output: dict[str, Any] | str,
        *,
        is_error: bool,
    ) -> None:
        if self._trace_path is None:
            return
        try:
            with self._trace_path.open("a", encoding="utf-8") as trace:
                trace.write(
                    json.dumps(
                        {
                            "name": endpoint,
                            "arguments": payload,
                            "output": output,
                            "is_error": is_error,
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            # Evidence is best-effort and must never change agent behavior. The
            # tools proxy remains a fallback when a trace mount is unavailable.
            return

    async def call(
        self,
        endpoint: str,
        *,
        _trace_payload: dict[str, Any] | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        traced_payload = {**payload, **(_trace_payload or {})}
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
            self._record(endpoint, traced_payload, result, is_error=False)
            return result
        except (httpx.HTTPError, ValueError) as exc:
            self._record(
                endpoint,
                traced_payload,
                f"The {endpoint.replace('_', ' ')} service is temporarily unavailable.",
                is_error=True,
            )
            raise ToolsAPIError(
                f"The {endpoint.replace('_', ' ')} service is temporarily unavailable."
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
