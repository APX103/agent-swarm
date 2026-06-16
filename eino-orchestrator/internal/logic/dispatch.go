package logic

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"eino-orchestrator/internal/types"
)

// dispatchViaSwarm 走 Swarm 的内部 dispatch 端点。
// POST {swarmBase}/api/internal/dispatch {agent_type, task, shared_dir}
// Swarm 的 dispatcher 会做 pool checkout + worker 激活 + 执行 + 归还。
func dispatchViaSwarm(swarmBase, agentType, task string, ctxInfo types.ContextInfo) (string, string) {
	body := map[string]any{
		"agent_type": agentType,
		"task":       task,
		"tenant_id":  "default",
	}
	if ctxInfo.SharedDir != "" {
		body["shared_dir"] = ctxInfo.SharedDir
	}
	raw, _ := json.Marshal(body)

	client := &http.Client{Timeout: 300 * time.Second}
	resp, err := client.Post(swarmBase+"/api/internal/dispatch", "application/json", bytes.NewReader(raw))
	if err != nil {
		return fmt.Sprintf("调用失败: %v", err), "failed"
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return fmt.Sprintf("dispatch http %d: %s", resp.StatusCode, string(respBody)), "failed"
	}

	var result struct {
		Success   bool     `json:"success"`
		Output    string   `json:"output"`
		Error     string   `json:"error"`
		Artifacts []string `json:"artifacts"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return string(respBody), "completed"
	}

	state := "completed"
	out := result.Output
	if !result.Success {
		state = "failed"
		if result.Error != "" {
			out = result.Error
		}
	}
	if out == "" {
		out = fmt.Sprintf("已执行（success=%v, artifacts=%d）", result.Success, len(result.Artifacts))
	}
	return out, state
}
