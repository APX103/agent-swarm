package handler

import (
	"encoding/json"
	"io"
	"log"
	"net/http"

	"github.com/google/uuid"
	"github.com/zeromicro/go-zero/rest"

	"eino-orchestrator/internal/svc"
)

// RegisterRoutes 把 A2A 路由挂到 go-zero server 上。
// A2A 是单 POST / + GET /.well-known/agent.json，不走 .api DSL。
func RegisterRoutes(server *rest.Server, svcCtx *svc.ServiceContext) {
	server.AddRoutes([]rest.Route{
		{Method: http.MethodGet, Path: "/.well-known/agent.json", Handler: AgentCardHandler(svcCtx)},
		{Method: http.MethodPost, Path: "/", Handler: JsonRpcHandler(svcCtx)},
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
			"capabilities": map[string]any{"streaming": false},
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
func JsonRpcHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {
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
		 handleMessageSend(w, req.ID, req.Params, svcCtx)
		case "tasks/get":
			// 简化：blocking 模式下 task 已在 message/send 返回，这里回个占位
			writeResult(w, req.ID, map[string]any{
				"id":     "n/a",
				"status": map[string]any{"state": "completed"},
			})
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

// handleMessageSend 解析 user message → 调 eino agent → 返回 A2A task。
func handleMessageSend(w http.ResponseWriter, id json.RawMessage, params json.RawMessage, svcCtx *svc.ServiceContext) {
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
	log.Printf("[eino] message/send: %q", truncateStr(userText, 120))

	// 调 eino ReAct agent
	resp, err := svcCtx.InvokeAgent(userText)
	if err != nil {
		log.Printf("[eino] agent 执行失败: %v", err)
		writeResult(w, id, map[string]any{
			"id":     uuid.NewString(),
			"status": map[string]any{"state": "failed"},
			"history": []map[string]any{
				{"role": "agent", "parts": []map[string]any{{"kind": "text", "text": "[eino error] " + err.Error()}}},
			},
		})
		return
	}

	log.Printf("[eino] message/send done, resp=%q", truncateStr(resp, 120))
	writeResult(w, id, map[string]any{
		"id":     uuid.NewString(),
		"status": map[string]any{"state": "completed"},
		"history": []map[string]any{
			{"role": "agent", "parts": []map[string]any{{"kind": "text", "text": resp}}},
		},
	})
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
	w.WriteHeader(http.StatusOK) // JSON-RPC 错误也是 200
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
