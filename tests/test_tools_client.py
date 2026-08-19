import json

import httpx
import pytest
from uber_voice_agent.tools_client import ToolsAPIError, ToolsClient


@pytest.mark.asyncio
async def test_client_posts_json_and_returns_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/lookup_rider_by_phone"
        assert request.headers["x-session-id"] == "room-1"
        return httpx.Response(200, json={"rider_id": "rdr_dana"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://tools") as raw:
        client = ToolsClient("http://tools", session_id="room-1", client=raw)
        result = await client.call("lookup_rider_by_phone", phone="+14155550101")
    assert result == {"rider_id": "rdr_dana"}


@pytest.mark.asyncio
async def test_client_turns_http_failures_into_safe_tool_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "postgres password leaked here"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://tools") as raw:
        client = ToolsClient("http://tools", session_id="room-1", client=raw)
        with pytest.raises(ToolsAPIError, match="temporarily unavailable") as exc:
            await client.call("get_ride_options", pickup_place_id="a")
    assert "password" not in str(exc.value)


@pytest.mark.asyncio
async def test_client_trace_records_direct_api_calls(tmp_path) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transferring": True})

    trace = tmp_path / "calls.jsonl"
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://tools") as raw:
        client = ToolsClient("http://tools", session_id="room-1", client=raw)
        client.enable_trace(str(trace))
        await client.call("transfer_to_human", reason="account suspended")

    record = json.loads(trace.read_text(encoding="utf-8"))
    assert record["name"] == "transfer_to_human"
    assert record["arguments"] == {"reason": "account suspended"}
    assert record["output"] == {"transferring": True}
