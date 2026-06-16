package svc

import (
	"context"
	"log"

	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/flow/agent/react"
	"github.com/cloudwego/eino/schema"
	openaimodel "github.com/cloudwego/eino-ext/components/model/openai"

	"eino-orchestrator/internal/config"
	"eino-orchestrator/internal/logic"
	"eino-orchestrator/internal/taskstore"
	"eino-orchestrator/internal/types"
)

// ContextInfo 是 types.ContextInfo 的别名（向后兼容 handler 调用）。
type ContextInfo = types.ContextInfo

// ServiceContext 持有 eino ReAct agent（编排大脑）。
type ServiceContext struct {
	Config config.Config
	Agent  *react.Agent
	// SwarmBase 给 dispatch tool 走 gateway 用
	SwarmBase string
}

func NewServiceContext(c config.Config) *ServiceContext {
	ctx := context.Background()

	timeout := c.GLM.Timeout
	if timeout == 0 {
		timeout = 120_000_000_000 // 120s
	}
	cm, err := openaimodel.NewChatModel(ctx, &openaimodel.ChatModelConfig{
		APIKey:  c.GLM.APIKey,
		BaseURL: c.GLM.BaseURL,
		Model:   c.GLM.Model,
		Timeout: timeout,
	})
	if err != nil {
		log.Fatalf("[eino] 初始化 GLM 模型失败: %v", err)
	}
	log.Printf("[eino] GLM 模型就绪: model=%s base=%s", c.GLM.Model, c.GLM.BaseURL)

	// dispatch tools 走 Swarm gateway（限制3），需要 SwarmBase
	tools := logic.BuildTools(c.Swarm.APIBase)
	log.Printf("[eino] dispatch tools 就绪: %d 个，走 gateway %s", len(tools), c.Swarm.APIBase)

	ag, err := react.NewAgent(ctx, &react.AgentConfig{
		ToolCallingModel: cm,
		ToolsConfig: compose.ToolsNodeConfig{
			Tools: tools,
		},
	})
	if err != nil {
		log.Fatalf("[eino] 初始化 ReAct agent 失败: %v", err)
	}
	log.Printf("[eino] ReAct agent 就绪，tools=%d", len(tools))

	return &ServiceContext{Config: c, Agent: ag, SwarmBase: c.Swarm.APIBase}
}

// InvokeAgent 调 eino ReAct agent（blocking 模式用）。
func (s *ServiceContext) InvokeAgent(userMessage string, ctxInfo ContextInfo) (string, error) {
	// 把 ctxInfo 存进 context，dispatch tool 从里面取 shared_dir
	ctx := context.WithValue(context.Background(), logic.CtxKeyContext{}, ctxInfo)
	msgs := []*schema.Message{
		{Role: schema.System, Content: systemPrompt()},
		{Role: schema.User, Content: userMessage},
	}
	resp, err := s.Agent.Generate(ctx, msgs)
	if err != nil {
		return "", err
	}
	return resp.Content, nil
}

// RunAgentWithProgress 后台跑 ReAct agent，dispatch tool 的进度写进 task store。
// 用于 non-blocking 模式：Swarm 轮询 tasks/get 时能看到 dispatch 进度。
func (s *ServiceContext) RunAgentWithProgress(taskID, userMessage string, ctxInfo ContextInfo, store *taskstore.Store) {
	// 把 taskID + store + ctxInfo 放进 context，dispatch tool 从里面取
	ctx := context.WithValue(context.Background(), logic.CtxKeyTaskID{}, taskID)
	ctx = context.WithValue(ctx, logic.CtxKeyStore{}, store)
	ctx = context.WithValue(ctx, logic.CtxKeyContext{}, ctxInfo)

	msgs := []*schema.Message{
		{Role: schema.System, Content: systemPrompt()},
		{Role: schema.User, Content: userMessage},
	}
	resp, err := s.Agent.Generate(ctx, msgs)
	if err != nil {
		log.Printf("[eino] task %s 失败: %v", taskID, err)
		store.Complete(taskID, "failed", "[eino error] "+err.Error())
		return
	}
	log.Printf("[eino] task %s 完成: %q", taskID, truncate(resp.Content, 120))
	store.Complete(taskID, "completed", resp.Content)
}

func systemPrompt() string {
	return "你是 Agent Swarm 平台的编排器（orchestrator）。你的职责是：" +
		"1) 分析用户需求，拆解成子任务；" +
		"2) 通过调用 dispatch_* 工具，把子任务分派给对应的专业 agent 执行；" +
		"3) 收集各 agent 的返回结果，汇总成一个完整的交付总结。" +
		"务必实际调用工具完成任务，不要凭空编造结果。最后用中文给出总结。"
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
