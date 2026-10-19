// Command model-gateway 是所有模型调用的唯一出口（设计书 3.3）。
// 由实施计划第 7 节的中档工作项实现，强档评审项为 R-02。
package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "model-gateway: not implemented yet")
	os.Exit(1)
}
