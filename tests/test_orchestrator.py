"""测试 Orchestrator 编排器"""
import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture
def mock_settings():
    from src.config import AgentCardDef, Settings, LLMConfig, ContainerPoolConfig, StorageConfig
    
    return Settings(
        llm=LLMConfig(
            default_model="test-model",
            default_base_url="https://test.example.com/v4",
            default_api_key="test-key",
        ),
        container_pool=ContainerPoolConfig(
            pool_size=3,
            base_port=9100,
        ),
        storage=StorageConfig(
            shared_output_base="/tmp/swarm-test",
        ),
        agent_cards=[
            AgentCardDef(id="frontend-ux-pro", name="Frontend", description="前端", skills=[]),
            AgentCardDef(id="backend-engineer", name="Backend", description="后端", skills=[]),
            AgentCardDef(id="general-agent", name="General", description="通用", skills=[]),
        ],
    )


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=MagicMock(
        container_id="c1",
        container_name="swarm-worker-0",
        port=9001,
    ))
    pool.return_container = AsyncMock()
    return pool


@pytest.fixture
def mock_task_mgr():
    tm = MagicMock()
    tm.get_task = MagicMock(return_value=MagicMock(
        task_id="t1", work_dir=MagicMock()
    ))
    tm.get_artifacts_dir = MagicMock(return_value=None)
    return tm


@pytest.fixture
def mock_openai():
    with patch("src.orchestrator.orchestrator.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        
        # 默认：LLM 返回 finalize 工具调用
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "完成"
        mock_response.choices[0].message.tool_calls = None
        mock_client.chat.completions.create.return_value = mock_response
        
        yield mock_client


@pytest.mark.asyncio
async def test_orchestrator_simple_response(mock_settings, mock_pool, mock_task_mgr, mock_openai):
    """Orchestrator 简单回复（无工具调用）"""
    from src.orchestrator.orchestrator import Orchestrator
    
    orch = Orchestrator(
        settings=mock_settings,
        pool_manager=mock_pool,
        task_manager=mock_task_mgr,
    )
    
    result = await orch.execute(
        task_id="t1",
        tenant_id="default",
        user_message="你好",
    )
    
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_orchestrator_dispatch_tool(mock_settings, mock_pool, mock_task_mgr):
    """Orchestrator 调用 dispatch_agent 工具"""
    from src.orchestrator.orchestrator import Orchestrator
    
    orch = Orchestrator(
        settings=mock_settings,
        pool_manager=mock_pool,
        task_manager=mock_task_mgr,
    )
    
    # 模拟 LLM 先调用 dispatch_agent，再调用 finalize
    calls = []
    
    def create_response_with_tools(tool_name, tool_args):
        resp = MagicMock()
        msg = MagicMock()
        msg.content = None
        msg.model_dump.return_value = {"role": "assistant", "content": None}
        
        tc = MagicMock()
        tc.id = f"call_{len(calls)}"
        tc.function.name = tool_name
        tc.function.arguments = json.dumps(tool_args)
        msg.tool_calls = [tc]
        
        resp.choices = [MagicMock()]
        resp.choices[0].message = msg
        
        # finalize 调用后无更多 tool_calls
        if tool_name != "finalize":
            msg2 = MagicMock()
            msg2.content = "任务完成"
            msg2.tool_calls = None
            msg2.model_dump.return_value = {"role": "assistant", "content": "任务完成"}
            resp2 = MagicMock()
            resp2.choices = [MagicMock()]
            resp2.choices[0].message = msg2
            calls.append((resp, resp2))
        else:
            calls.append((resp,))
        
        return calls[len(calls) - 1][0]
    
    call_count = [0]
    
    def mock_create(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # dispatch_agent
            return create_response_with_tools("dispatch_agent", {
                "agent_type": "frontend-ux-pro",
                "task": "写一个登录页面",
            })
        else:
            # finalize
            return create_response_with_tools("finalize", {
                "summary": "前端登录页面已完成",
            })
    
    orch.client.chat.completions.create = mock_create
    
    result = await orch.execute(
        task_id="t1",
        tenant_id="default",
        user_message="帮我写一个登录页面",
    )
    
    assert isinstance(result, str)
    assert "前端登录页面已完成" in result or "登录页面" in result
    # 验证容器被 checkout 和 return
    mock_pool.checkout.assert_called_once()
    mock_pool.return_container.assert_called_once()


@pytest.mark.asyncio
async def test_tool_list_artifacts(mock_settings, mock_pool, mock_task_mgr, mock_openai):
    """测试 list_artifacts 工具"""
    from src.orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(
        settings=mock_settings,
        pool_manager=mock_pool,
        task_manager=mock_task_mgr,
    )
    orch._current_task_id = "t1"

    # mock get_artifacts_dir to return a real dir
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_task_mgr.get_artifacts_dir.return_value = Path(tmpdir)
        (Path(tmpdir) / "frontend").mkdir(exist_ok=True)
        result = await orch._tool_list_artifacts({})
        assert result is not None


@pytest.mark.asyncio
async def test_tool_read_artifacts_not_found(mock_settings, mock_pool, mock_task_mgr, mock_openai):
    """测试 read_artifacts 文件不存在"""
    from src.orchestrator.orchestrator import Orchestrator
    
    orch = Orchestrator(
        settings=mock_settings,
        pool_manager=mock_pool,
        task_manager=mock_task_mgr,
    )
    orch._current_task_id = "t1"
    
    result = await orch._tool_read_artifacts({"file_path": "nonexistent.txt"})
    assert "不存在" in result or "未找到" in result


@pytest.mark.asyncio
async def test_orchestrator_uses_injected_dispatcher(mock_settings, mock_pool, mock_task_mgr):
    """R2.8: an injected Dispatcher is used (e.g. an external agent becomes a candidate)."""
    from src.orchestrator.orchestrator import Orchestrator
    from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
    from src.dispatcher.base import DispatchAttempt, DispatchTarget

    class FakeExternalBackend:
        async def candidates(self, agent_type, agent_id=None):
            return [DispatchTarget(kind="external", agent_type=agent_type, agent_id="ext1")]

        async def invoke(self, target, request):
            return DispatchAttempt(target=target, success=True, output="EXTERNAL-OK")

        async def health_check(self, target):
            return True

    dispatcher = Dispatcher([FakeExternalBackend()], DispatcherConfig(health_precheck=False))
    orch = Orchestrator(
        settings=mock_settings,
        pool_manager=mock_pool,
        task_manager=mock_task_mgr,
        dispatcher=dispatcher,
    )
    orch._current_task_id = "t1"

    result = await orch._tool_dispatch_agent({"agent_type": "frontend-ux-pro", "task": "x"})
    assert "EXTERNAL-OK" in result
    assert "completed" in result


@pytest.mark.asyncio
async def test_orchestrator_logs_carry_trace_id(mock_settings, mock_pool, mock_task_mgr, caplog):
    """W4.3: logs emitted during orchestration carry the active trace id."""
    import logging
    from src.observability.trace import TraceIdFilter, set_trace_id
    from src.orchestrator.orchestrator import Orchestrator

    with patch("src.orchestrator.orchestrator.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "done"
        mock_response.choices[0].message.tool_calls = None
        mock_client.chat.completions.create.return_value = mock_response
        MockOpenAI.return_value = mock_client

        orch = Orchestrator(
            settings=mock_settings, pool_manager=mock_pool, task_manager=mock_task_mgr
        )
        caplog.set_level(logging.DEBUG, logger="src.orchestrator.orchestrator")
        caplog.handler.addFilter(TraceIdFilter())

        set_trace_id("TRACE-XYZ")
        try:
            await orch.execute("t1", "default", "hi")
        finally:
            set_trace_id(None)

    assert any(getattr(r, "trace_id", None) == "TRACE-XYZ" for r in caplog.records)
