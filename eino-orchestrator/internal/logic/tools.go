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

	"eino-orchestrator/internal/taskstore"
	"eino-orchestrator/internal/types"
)

type CtxKeyContext struct{}
type CtxKeyTaskID struct{}
type CtxKeyStore struct{}

type AgentInfo struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

func fetchAgents(swarmBase string) []AgentInfo {
	client := &http.Client{Timeout: 10 * time.Second}
	for _, path := range []string{"/api/agents", "/api/v1/agents"} {
		resp, err := client.Get(swarmBase + path)
		if err != nil {
			continue
		}
		var list []AgentInfo
		err2 := json.NewDecoder(resp.Body).Decode(&list)
		resp.Body.Close()
		if err2 == nil && len(list) > 0 {
			return list
		}
	}
	return nil
}

type DispatchArgs struct {
	Task string `json:"task" jsonschema_description:"要交给该 agent 完成的具体任务描述"`
}

type DispatchResult struct {
	Result string `json:"result"`
}

func BuildTools(swarmBase string) []tool.BaseTool {
	agents := fetchAgents(swarmBase)
	if len(agents) == 0 {
		log.Printf("[eino] 未从 %s 拉到 agent，回退到内置约定", swarmBase)
		agents = []AgentInfo{
			{ID: "frontend-ux-pro", Name: "Frontend Engineer", Description: "前端开发"},
			{ID: "backend-engineer", Name: "Backend Engineer", Description: "后端开发"},
		}
	}

	var tools []tool.BaseTool
	for _, a := range agents {
		a := a
		sb := swarmBase
		toolName := "dispatch_" + a.ID
		fn := func(ctx context.Context, args DispatchArgs) (DispatchResult, error) {
			ctxInfo, _ := ctx.Value(CtxKeyContext{}).(types.ContextInfo)
			taskID, _ := ctx.Value(CtxKeyTaskID{}).(string)
			store, _ := ctx.Value(CtxKeyStore{}).(*taskstore.Store)

			log.Printf("[eino] tool %s -> swarm dispatch, task=%q shared_dir=%q",
				toolName, truncate(args.Task, 80), ctxInfo.SharedDir)

			if store != nil && taskID != "" {
				store.AppendProgress(taskID, map[string]any{
					"step":  len(getProgress(store, taskID)),
					"type":  "dispatch_start",
					"agent": a.Name,
					"task":  truncate(args.Task, 100),
				})
			}

			result, state := dispatchViaSwarm(sb, a.ID, args.Task, ctxInfo)

			if store != nil && taskID != "" {
				store.AppendProgress(taskID, map[string]any{
					"step":   len(getProgress(store, taskID)),
					"type":   "dispatch_done",
					"agent":  a.Name,
					"state":  state,
					"result": truncate(result, 200),
				})
			}
			log.Printf("[eino] tool %s done, state=%s, out=%q", toolName, state, truncate(result, 100))
			return DispatchResult{Result: result}, nil
		}
		desc := fmt.Sprintf("把任务分派给 %s（%s）。当需要%s相关的工作时调用。", a.Name, a.Description, a.Name)
		t, err := utils.InferTool(toolName, desc, fn)
		if err != nil {
			log.Printf("[eino] InferTool(%s) 失败: %v", toolName, err)
			continue
		}
		tools = append(tools, t)
		if len(tools) >= 3 {
			break
		}
	}
	log.Printf("[eino] 共生成 %d 个 dispatch tool（走 swarm internal dispatch）", len(tools))
	return tools
}

func getProgress(store *taskstore.Store, taskID string) []map[string]any {
	t, ok := store.Get(taskID)
	if !ok {
		return nil
	}
	return t.Progress
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
