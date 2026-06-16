package config

import (
	"time"

	"github.com/zeromicro/go-zero/rest"
)

// Config — eino-orchestrator 的配置，映射 etc/*.yaml。
type Config struct {
	rest.RestConf

	// GLM / OpenAI 兼容模型配置
	GLM GLMConfig

	// Swarm 平台地址（用于拉取 agent 列表、调 worker）
	Swarm SwarmConfig
}

type GLMConfig struct {
	BaseURL string
	APIKey  string
	Model   string
	Timeout time.Duration // 默认 120s
}

type SwarmConfig struct {
	// Swarm 后端地址，如 http://localhost:9000
	APIBase string
}
