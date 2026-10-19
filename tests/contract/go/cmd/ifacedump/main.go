// Command ifacedump 列出给定 Go 包目录里每个导出接口的方法名，以 JSON 输出：
//
//	{"<目录>": {"<接口名>": ["方法", ...]}}
//
// 只解析源码，不做类型检查，供 tests/contract/test_provider_parity.py 比对两种语言的 Provider 接口。
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

func dump(dir string) (map[string][]string, error) {
	files, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	interfaces := map[string][]string{}
	fset := token.NewFileSet()
	for _, path := range files {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return nil, err
		}
		ast.Inspect(file, func(n ast.Node) bool {
			spec, ok := n.(*ast.TypeSpec)
			if !ok || !spec.Name.IsExported() {
				return true
			}
			iface, ok := spec.Type.(*ast.InterfaceType)
			if !ok {
				return true
			}
			methods := []string{}
			for _, field := range iface.Methods.List {
				for _, name := range field.Names {
					methods = append(methods, name.Name)
				}
			}
			interfaces[spec.Name.Name] = methods
			return true
		})
	}
	return interfaces, nil
}

func main() {
	out := map[string]map[string][]string{}
	for _, dir := range os.Args[1:] {
		interfaces, err := dump(dir)
		if err != nil {
			fmt.Fprintln(os.Stderr, "ifacedump:", err)
			os.Exit(1)
		}
		out[filepath.ToSlash(dir)] = interfaces
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, "ifacedump:", err)
		os.Exit(1)
	}
}
