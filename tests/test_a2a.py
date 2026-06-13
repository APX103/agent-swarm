"""测试 A2A 客户端"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json


@pytest.fixture
def mock_httpx_client():
    with patch("src.common.a2a_client.httpx.AsyncClient") as MockClient:
        mock_instance = AsyncMock()
        MockClient.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def a2a_client(mock_httpx_client):
    from src.common.a2a_client import A2AClient
    return A2AClient("http://localhost:9001", timeout=10.0)


@pytest.mark.asyncio
async def test_get_agent_card(a2a_client, mock_httpx_client):
    """获取 AgentCard"""
    expected_card = {
        "name": "Test Agent",
        "version": "1.0.0",
        "skills": [],
    }
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = expected_card
    mock_httpx_client.get = AsyncMock(return_value=mock_resp)
    
    card = await a2a_client.get_agent_card()
    assert card is not None
    assert card["name"] == "Test Agent"
    mock_httpx_client.get.assert_called_once_with(
        "http://localhost:9001/.well-known/agent.json"
    )


@pytest.mark.asyncio
async def test_get_agent_card_failure(a2a_client, mock_httpx_client):
    """获取 AgentCard 失败"""
    mock_httpx_client.get = AsyncMock(side_effect=Exception("Connection refused"))
    
    card = await a2a_client.get_agent_card()
    assert card is None


@pytest.mark.asyncio
async def test_send_message_blocking(a2a_client, mock_httpx_client):
    """发送消息（阻塞模式）"""
    from src.common.a2a_client import A2AMessage
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "id": "task-abc",
            "status": {"state": "completed"},
            "history": [
                {"role": "user", "parts": [{"kind": "text", "text": "写一个登录页面"}]},
                {"role": "agent", "parts": [{"kind": "text", "text": "任务完成"}], "messageId": "msg-1"},
            ],
        }
    }
    mock_httpx_client.post = AsyncMock(return_value=mock_resp)
    
    msg = A2AMessage(role="user", text="写一个登录页面")
    result = await a2a_client.send_message(msg, blocking=True)
    
    assert result is not None
    assert result.task_id == "task-abc"
    assert result.state == "completed"
    assert "任务完成" in result.message
    
    # 验证请求格式
    call_args = mock_httpx_client.post.call_args
    request_json = call_args[1]["json"]
    assert request_json["method"] == "message/send"
    assert request_json["params"]["message"]["role"] == "user"
    assert request_json["params"]["configuration"]["blocking"] is True


@pytest.mark.asyncio
async def test_send_message_error(a2a_client, mock_httpx_client):
    """发送消息失败"""
    from src.common.a2a_client import A2AMessage
    
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_httpx_client.post = AsyncMock(return_value=mock_resp)
    
    msg = A2AMessage(role="user", text="test")
    result = await a2a_client.send_message(msg)
    
    assert result is None


@pytest.mark.asyncio
async def test_get_task(a2a_client, mock_httpx_client):
    """查询任务状态"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "id": "task-abc",
            "status": {"state": "working"},
        }
    }
    mock_httpx_client.post = AsyncMock(return_value=mock_resp)
    
    task = await a2a_client.get_task("task-abc")
    
    assert task is not None
    assert task.state == "working"


@pytest.mark.asyncio
async def test_message_format(a2a_client, mock_httpx_client):
    """验证 A2A 消息格式"""
    from src.common.a2a_client import A2AMessage
    
    msg = A2AMessage(role="user", text="Hello")
    d = msg.to_dict()
    
    assert d["role"] == "user"
    assert len(d["parts"]) == 1
    assert d["parts"][0]["kind"] == "text"
    assert d["parts"][0]["text"] == "Hello"
    assert "messageId" in d
