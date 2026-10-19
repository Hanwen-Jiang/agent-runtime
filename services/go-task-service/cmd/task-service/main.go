// Command task-service 是 Control Plane 的入口：Task API 的 gRPC 服务与 Session 工作流的 Worker
// （设计书 3.1）。由强档实施计划的 S-03 实现。
package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "task-service: not implemented yet (strong-tier plan S-03)")
	os.Exit(1)
}
