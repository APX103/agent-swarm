package logic

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/components/tool/utils"

	"eino-orchestrator/internal/a2a"
)

// AgentInfo 是从 Swarm /api/agents 拿到的内置 agent 描述。
type AgentInfo struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	// Swarm 内置 worker 都监听在 pool 分配的端口；前端 card 没有 endpoint，
	// 但内置 worker 固定在 localhost:9001~9005（pool_size=5）。我们在 BuildTools
	// 里按约定推导端口。
}

// fetchAgents 拉 Swarm 的 agent 列表（内置 card + 外部注册的）。
func fetchAgents(swarmBase string) ([]AgentInfo, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	// 先试 /api/agents（内置 card），再试 /api/v1/agents（外部注册）
	var agents []AgentInfo
	for _, path := range []string{"/api/agents", "/api/v1/agents"} {
		resp, err := client.Get(swarmBase + path)
		if err != nil {
			continue
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			continue
		}
		var list []AgentInfo
		if err := json.NewDecoder(resp.Body).Decode(&list); err == nil && len(list) > 0 {
			agents = append(agents, list...)
		}
	}
	return agents, nil
}

// DispatchArgs 是 dispatch tool 的入参（LLM 填）。
type DispatchArgs struct {
	Task string `json:"task" jsonschema_description:"要交给该 agent 完成的具体任务描述（中文或英文）"`
}

// DispatchResult 是 dispatch tool 的返回。
type DispatchResult struct {
	Result string `json:"result"`
}

// BuildTools 为每个 Swarm agent 生成一个 eino InvokableTool。
// 内置 worker 端口按 9001 + index 推导（pool 顺序）；外部 agent 用注册时的 endpoint。
func BuildTools(swarmBase string) []tool.BaseTool {
	agents, err := fetchAgents(swarmBase)
	if err != nil || len(agents) == 0 {
		log.Printf("[eino] 未从 %s 拉到 agent，回退到内置 frontend/backend 约定", swarmBase)
		agents = []AgentInfo{
			{ID: "frontend-ux-pro", Name: "Frontend Engineer", Description: "前端开发"},
			{ID: "backend-engineer", Name: "Backend Engineer", Description: "后端开发"},
		}
	}

	var tools []tool.BaseTool
	// demo 简化：所有 dispatch tool 都指向 9001（Swarm warm pool 的 worker-0）。
	// 生产环境应该让 eino 走 Swarm 的 /api/v1/agents/{id}/invoke 端点（带 agent_id 直选）。
	workerEndpoint := "http://localhost:9001"
	for _, a := range agents {
		// 内置 worker 池当前只有 1 个容器（9001），都指向它。
		// 真实部署会改走 Swarm gateway dispatch。
		toolName := "dispatch_" + a.ID
		agentName := a.Name
		fn := func(ctx context.Context, args DispatchArgs) (DispatchResult, error) {
			log.Printf("[eino] tool %s -> %s, task=%q", toolName, workerEndpoint, truncate(args.Task, 80))
			c := a2a.NewClient(workerEndpoint)
			task, err := c.SendMessage(ctx, args.Task)
			if err != nil {
				return DispatchResult{Result: fmt.Sprintf("调用 %s 失败: %v", agentName, err)}, nil
			}
			out := task.AgentText()
			if out == "" {
				out = fmt.Sprintf("%s 已执行，状态=%s（无文本输出）", agentName, task.State())
			}
			log.Printf("[eino] tool %s <- %s done, state=%s, out=%q", toolName, workerEndpoint, task.State(), truncate(out, 100))
			return DispatchResult{Result: out}, nil
		}
		desc := fmt.Sprintf("把任务分派给 %s（%s）。当需要%s相关的工作时调用。", a.Name, a.Description, a.Name)
		t, err := utils.InferTool(toolName, desc, fn)
		if err != nil {
			log.Printf("[eino] InferTool(%s) 失败: %v", toolName, err)
			continue
		}
		tools = append(tools, t)
		if len(tools) >= 3 {
			break // demo 只用前 3 个 agent（frontend/backend/fullstack）
		}
	}
	log.Printf("[eino] 共生成 %d 个 dispatch tool，全部指向 %s", len(tools), workerEndpoint)
	return tools
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
