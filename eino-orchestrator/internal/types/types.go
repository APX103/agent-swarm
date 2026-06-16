// Package types 放跨包共享的类型，打破 import cycle。
package types

// ContextInfo 携带 Swarm 传来的 session 上下文。
// dispatch tools 用 SharedDir 让 worker 把产物写到正确的 task 目录。
type ContextInfo struct {
	SharedDir   string // task 的共享产物目录
	SwarmTaskID string // Swarm 侧的 task_id
	TenantID    string
}
