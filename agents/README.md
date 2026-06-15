# 声明式 Agent 注册

放 YAML 文件到这个目录，编排器启动时自动注册。

## 格式
```yaml
name: my-chatbot          # 必填
endpoint: http://host:port  # 必填
protocol: openai          # openai | cli | mcp | a2a | http
skills: [summarize, translate]  # 编排器按 skill 选中
# 协议特定字段（可选，默认用 endpoint）：
# base_url: http://host:port   # openai/a2a 用
# model: gpt-4                 # openai 用
# command: python3             # cli 用
# server_url: http://host:port # mcp 用
```

启动时自动注册到 registry + adapter_manager。也可以运行时 POST /api/v1/agents/register 动态注册。
