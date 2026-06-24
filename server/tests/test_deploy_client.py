import pytest
import httpx
from server.deploy.client import DeployControllerClient, DeployControllerUnavailable


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_status_returns_body():
    def handler(req):
        return httpx.Response(200, json={"pending": {"behind": True}, "deploy": {"running": False}})
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    body = await c.get_status()
    assert body["pending"]["behind"] is True


@pytest.mark.asyncio
async def test_start_returns_status_and_body():
    def handler(req):
        return httpx.Response(409, json={"started": False, "reason": "in progress"})
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    code, body = await c.start()
    assert code == 409 and body["started"] is False


@pytest.mark.asyncio
async def test_connect_error_raises_unavailable():
    def handler(req):
        raise httpx.ConnectError("no sock")
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    with pytest.raises(DeployControllerUnavailable):
        await c.get_status()
