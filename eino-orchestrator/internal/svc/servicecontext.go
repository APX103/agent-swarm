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
)

// ServiceContext 持有 eino ReAct agent（编排大脑）。
type ServiceContext struct {
	Config config.Config
	Agent  *react.Agent
}

func NewServiceContext(c config.Config) *ServiceContext {
	ctx := context.Background()

	// 1. GLM chat model（OpenAI 兼容）
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

	// 2. 从 Swarm 平台拉 agent 列表，生成 dispatch tools
	tools := logic.BuildTools(c.Swarm.APIBase)

	// 3. 组装 ReAct agent
	//    ToolsConfig 里塞 tools；eino 会自动把 tool 信息告诉 LLM 做 function calling。
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

	return &ServiceContext{Config: c, Agent: ag}
}

// InvokeAgent 调 eino ReAct agent，返回最终文本。
// system prompt 让 eino 扮演编排器：拆解任务、调用 dispatch tool、汇总。
func (s *ServiceContext) InvokeAgent(userMessage string) (string, error) {
	ctx := context.Background()
	msgs := []*schema.Message{
		{
			Role: schema.System,
			Content: "你是 Agent Swarm 平台的编排器（orchestrator）。你的职责是：" +
				"1) 分析用户需求，拆解成子任务；" +
				"2) 通过调用 dispatch_* 工具，把子任务分派给对应的专业 agent 执行；" +
				"3) 收集各 agent 的返回结果，汇总成一个完整的交付总结。" +
				"务必实际调用工具完成任务，不要凭空编造结果。最后用中文给出总结。",
		},
		{Role: schema.User, Content: userMessage},
	}
	resp, err := s.Agent.Generate(ctx, msgs)
	if err != nil {
		return "", err
	}
	return resp.Content, nil
}
