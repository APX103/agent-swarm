// Package a2a 是 Go 版 A2A JSON-RPC 客户端，用于 eino orchestrator 调用 Swarm worker。
// 协议与 src/common/a2a_client.py 一致：POST 到 worker 根 URL，body 是 JSON-RPC 2.0。
package a2a

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
)

// Task 是 A2A 任务的简化视图（Swarm worker 返回的字段）。
type Task struct {
	ID       string                 `json:"id"`
	Status   map[string]any         `json:"status"` // {"state": "completed"|"failed"|"working"}
	History  []map[string]any       `json:"history"`
	Progress []map[string]any       `json:"progress"`
	Extras   map[string]interface{} `json:"-"` // 其余字段兜底
}

// State 提取状态字符串。
func (t *Task) State() string {
	if s, ok := t.Status["state"].(string); ok {
		return s
	}
	return "unknown"
}

// AgentText 提取 worker 返回的 agent 文本（history 里最后一个 role=agent 的 text）。
func (t *Task) AgentText() string {
	for i := len(t.History) - 1; i >= 0; i-- {
		h := t.History[i]
		if role, _ := h["role"].(string); role != "agent" {
			continue
		}
		parts, _ := h["parts"].([]any)
		for _, p := range parts {
			pm, _ := p.(map[string]any)
			if kind, _ := pm["kind"].(string); kind == "text" {
				if txt, _ := pm["text"].(string); txt != "" {
					return txt
				}
			}
		}
	}
	return ""
}

// Client 是 A2A HTTP 客户端。
type Client struct {
	HTTP    *http.Client
	BaseURL string
}

func NewClient(baseURL string) *Client {
	return &Client{
		HTTP:    &http.Client{Timeout: 300 * time.Second},
		BaseURL: baseURL,
	}
}

// SendMessage 发一个 blocking message/send，等 worker 跑完返回 task。
func (c *Client) SendMessage(ctx context.Context, text string) (*Task, error) {
	req := map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "message/send",
		"params": map[string]any{
			"message": map[string]any{
				"role":      "user",
				"messageId": uuid.NewString(),
				"parts":     []map[string]any{{"kind": "text", "text": text}},
			},
			"configuration": map[string]any{"blocking": true},
		},
	}
	var resp map[string]any
	if err := c.post(ctx, req, &resp); err != nil {
		return nil, err
	}
	if e, ok := resp["error"]; ok {
		return nil, fmt.Errorf("a2a error: %v", e)
	}
	return parseTask(resp["result"])
}

func (c *Client) post(ctx context.Context, body any, out *map[string]any) error {
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("a2a http %d: %s", resp.StatusCode, string(data))
	}
	return json.Unmarshal(data, out)
}

func parseTask(v any) (*Task, error) {
	if v == nil {
		return nil, fmt.Errorf("nil result")
	}
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var t Task
	if err := json.Unmarshal(raw, &t); err != nil {
		return nil, err
	}
	return &t, nil
}
