// Package taskstore 是 eino orchestrator 的内存 task 存储。
// 用于支持 A2A non-blocking 模式：message/send 立即返回 task_id，
// tasks/get 轮询拿进度，终态后 task 留在内存里供查询。
package taskstore

import (
	"sync"
	"time"
)

// Task 是 eino orchestrator 内部维护的任务状态。
type Task struct {
	ID         string
	State      string // "working" | "completed" | "failed"
	Message    string // 最终 agent 文本
	Progress   []map[string]any
	StartedAt  time.Time
	FinishedAt time.Time
}

// Store 是线程安全的内存 task 存储。
type Store struct {
	mu sync.RWMutex
	m  map[string]*Task
}

func New() *Store {
	return &Store{m: make(map[string]*Task)}
}

func (s *Store) Create(id string) *Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := &Task{ID: id, State: "working", StartedAt: time.Now(), Progress: []map[string]any{}}
	s.m[id] = t
	return t
}

func (s *Store) Get(id string) (*Task, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	t, ok := s.m[id]
	return t, ok
}

// AppendProgress 给 task 追加一个进度条目（线程安全）。
func (s *Store) AppendProgress(id string, entry map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if t, ok := s.m[id]; ok {
		t.Progress = append(t.Progress, entry)
	}
}

// Complete 标记 task 终态。
func (s *Store) Complete(id string, state string, message string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if t, ok := s.m[id]; ok {
		t.State = state
		t.Message = message
		t.FinishedAt = time.Now()
	}
}
