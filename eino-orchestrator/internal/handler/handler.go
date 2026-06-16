package handler

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"

	"github.com/google/uuid"
	"github.com/zeromicro/go-zero/rest"

	"eino-orchestrator/internal/svc"
	"eino-orchestrator/internal/taskstore"
)

// RegisterRoutes 把 A2A 路由挂到 go-zero server 上。
func RegisterRoutes(server *rest.Server, svcCtx *svc.ServiceContext) {
	store := taskstore.New()
	server.AddRoutes([]rest.Route{
		{Method: http.MethodGet, Path: "/.well-known/agent.json", Handler: AgentCardHandler(svcCtx)},
		{Method: http.MethodPost, Path: "/", Handler: JsonRpcHandler(svcCtx, store)},
	})
}

// AgentCardHandler 返回 A2A AgentCard。
func AgentCardHandler(_ *svc.ServiceContext) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		card := map[string]any{
			"name":         "eino-orchestrator",
			"description":  "基于 eino ReAct agent 的编排器，可调度 Swarm 平台的其他 agent",
			"url":          "http://localhost:9020",
			"version":      "0.1.0",
			"capabilities": map[string]any{"streaming": true},
			"skills": []map[string]any{
				{"id": "orchestration", "name": "Multi-Agent Orchestration",
					"description": "拆解任务并分派给多个子 agent 执行"},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(card)
	}
}

// JsonRpcHandler 处理 A2A JSON-RPC 2.0。
func JsonRpcHandler(svcCtx *svc.ServiceContext, store *taskstore.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req struct {
			Jsonrpc string          `json:"jsonrpc"`
			ID      json.RawMessage `json:"id"`
			Method  string          `json:"method"`
			Params  json.RawMessage `json:"params"`
		}
		if err := json.Unmarshal(body, &req); err != nil {
			writeErr(w, json.RawMessage("1"), -32700, "Parse error: "+err.Error())
			return
		}

		switch req.Method {
		case "message/send":
			handleMessageSend(w, req.ID, req.Params, svcCtx, store)
		case "tasks/get":
			handleTasksGet(w, req.ID, req.Params, store)
		case "tasks/cancel":
			writeResult(w, req.ID, map[string]any{
				"id":     "n/a",
				"status": map[string]any{"state": "canceled"},
			})
		default:
			writeErr(w, req.ID, -32601, "Method not found: "+req.Method)
		}
	}
}

// handleMessageSend 支持 blocking 和 non-blocking 两种模式。
func handleMessageSend(w http.ResponseWriter, id json.RawMessage, params json.RawMessage, svcCtx *svc.ServiceContext, store *taskstore.Store) {
	var p struct {
		Message       map[string]any `json:"message"`
		Configuration map[string]any `json:"configuration"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		writeErr(w, id, -32602, "Invalid params: "+err.Error())
		return
	}
	userText := extractText(p.Message)
	if userText == "" {
		writeErr(w, id, -32602, "Empty message")
		return
	}

	taskID := uuid.NewString()
	blocking := true
	if v, ok := p.Configuration["blocking"]; ok {
		if b, ok2 := v.(bool); ok2 {
			blocking = b
		}
	}

	// 解析 session 上下文（限制1：Swarm 传过来的 work_dir/task_id/tenant_id）
	ctxInfo := svc.ContextInfo{}
	if c, ok := p.Configuration["shared_dir"].(string); ok {
		ctxInfo.SharedDir = c
	}
	if c, ok := p.Configuration["task_id"].(string); ok {
		ctxInfo.SwarmTaskID = c
	}
	if c, ok := p.Configuration["tenant_id"].(string); ok {
		ctxInfo.TenantID = c
	}

	log.Printf("[eino] message/send: task=%s blocking=%v shared_dir=%q msg=%q",
		taskID, blocking, ctxInfo.SharedDir, truncateStr(userText, 100))

	if blocking {
		// blocking：同步跑完，直接返回终态
		resp, err := svcCtx.InvokeAgent(userText, ctxInfo)
		if err != nil {
			writeResult(w, id, taskResult(taskID, "failed", err.Error(), nil))
			return
		}
		writeResult(w, id, taskResult(taskID, "completed", resp, nil))
		return
	}

	// non-blocking：立即返回 working task，后台跑 ReAct
	t := store.Create(taskID)
	_ = t
	go func() {
		// 每次调 dispatch tool 会通过 svcCtx 的 progress 回调写 task store
		svcCtx.RunAgentWithProgress(taskID, userText, ctxInfo, store)
	}()

	writeResult(w, id, taskResult(taskID, "working", "", nil))
}

// handleTasksGet 返回 task 当前状态 + progress（供 Swarm 轮询）。
func handleTasksGet(w http.ResponseWriter, id json.RawMessage, params json.RawMessage, store *taskstore.Store) {
	var p struct {
		ID string `json:"id"`
	}
	_ = json.Unmarshal(params, &p)
	t, ok := store.Get(p.ID)
	if !ok {
		writeErr(w, id, -32001, "Task not found: "+p.ID)
		return
	}
	writeResult(w, id, taskResult(t.ID, t.State, t.Message, t.Progress))
}

// taskResult 构造 A2A task 响应结构（和 Swarm worker 的 tasks/get 返回一致）。
func taskResult(taskID, state, agentText string, progress []map[string]any) map[string]any {
	history := []map[string]any{}
	if agentText != "" {
		history = append(history, map[string]any{
			"role":  "agent",
			"parts": []map[string]any{{"kind": "text", "text": agentText}},
		})
	}
	if progress == nil {
		progress = []map[string]any{}
	}
	return map[string]any{
		"id":       taskID,
		"status":   map[string]any{"state": state},
		"history":  history,
		"progress": progress,
	}
}

func extractText(msg map[string]any) string {
	if msg == nil {
		return ""
	}
	parts, _ := msg["parts"].([]any)
	out := ""
	for _, pp := range parts {
		pm, _ := pp.(map[string]any)
		if kind, _ := pm["kind"].(string); kind == "text" {
			if t, _ := pm["text"].(string); t != "" {
				out += t
			}
		}
	}
	return out
}

func writeResult(w http.ResponseWriter, id json.RawMessage, result any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"jsonrpc": "2.0", "id": id, "result": result,
	})
}

func writeErr(w http.ResponseWriter, id json.RawMessage, code int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"jsonrpc": "2.0", "id": id,
		"error": map[string]any{"code": code, "message": message},
	})
}

func truncateStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// 避免未用 import 报错
var _ = context.Background