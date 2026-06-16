package main

import (
	"flag"

	"github.com/zeromicro/go-zero/core/conf"
	"github.com/zeromicro/go-zero/rest"

	"eino-orchestrator/internal/config"
	"eino-orchestrator/internal/handler"
	"eino-orchestrator/internal/svc"
)

var configFile = flag.String("f", "etc/eino-orchestrator.yaml", "配置文件路径")

func main() {
	flag.Parse()

	var c config.Config
	conf.MustLoad(*configFile, &c)

	server := rest.MustNewServer(c.RestConf)
	defer server.Stop()

	svcCtx := svc.NewServiceContext(c)
	handler.RegisterRoutes(server, svcCtx)

	server.Start()
}
