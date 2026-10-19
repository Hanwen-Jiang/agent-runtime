package session

// S-03 验收：工作流代码必须确定（计划 S-03 难点“确定性（Go）”，附录 K.2）。Temporal 会重放事件历史，
// 工作流代码里不用 time.Now、原生 goroutine、channel 与 select，不做 I/O，不依赖 map 的遍历顺序。
//
// 检查本包除 activities.go 以外的非测试文件：activity 本来就做 I/O，单独放在 activities.go。
// map 遍历用语法层面的近似判断：遍历声明为 map 的字段、变量、参数，或 proto 的 map 字段 getter，
// 都要放进名字以 sorted 开头的辅助函数里（取出键后排序）。

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"

	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

var forbiddenImports = map[string]string{
	"math/rand":    "用 workflow.SideEffect 或 activity",
	"math/rand/v2": "用 workflow.SideEffect 或 activity",
	"crypto/rand":  "用 workflow.SideEffect 或 activity",
	"os":           "工作流不做 I/O",
	"os/exec":      "工作流不做 I/O",
	"net":          "工作流不做 I/O",
	"net/http":     "工作流不做 I/O",
	"io/ioutil":    "工作流不做 I/O",
	"log":          "用 workflow.GetLogger",
	"log/slog":     "用 workflow.GetLogger",
	"sync":         "用 workflow.Mutex 之类的确定性原语",
	"sync/atomic":  "用 workflow.Mutex 之类的确定性原语",
}

var forbiddenTimeFuncs = map[string]bool{
	"Now": true, "Since": true, "Until": true, "Sleep": true, "After": true,
	"AfterFunc": true, "Tick": true, "NewTimer": true, "NewTicker": true,
}

func workflowFiles(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, p := range paths {
		if strings.HasSuffix(p, "_test.go") || p == "activities.go" {
			continue
		}
		out = append(out, p)
	}
	if len(out) == 0 {
		t.Fatal("没有找到工作流代码")
	}
	return out
}

// protoMapGetters 返回已注册的 proto 消息里 map 字段的 Go getter 名，例如 GetSubmitted。
func protoMapGetters() map[string]bool {
	getters := map[string]bool{}
	protoregistry.GlobalTypes.RangeMessages(func(mt protoreflect.MessageType) bool {
		fields := mt.Descriptor().Fields()
		for i := 0; i < fields.Len(); i++ {
			if f := fields.Get(i); f.IsMap() {
				getters["Get"+camel(string(f.Name()))] = true
			}
		}
		return true
	})
	return getters
}

func camel(s string) string {
	var b strings.Builder
	for _, part := range strings.Split(s, "_") {
		if part != "" {
			b.WriteString(strings.ToUpper(part[:1]) + part[1:])
		}
	}
	return b.String()
}

// mapNames 收集声明为 map 的字段、变量与参数名。
func mapNames(files []*ast.File) map[string]bool {
	names := map[string]bool{}
	isMap := func(e ast.Expr) bool {
		switch v := e.(type) {
		case *ast.MapType:
			return true
		case *ast.CompositeLit:
			_, ok := v.Type.(*ast.MapType)
			return ok
		case *ast.CallExpr:
			if id, ok := v.Fun.(*ast.Ident); ok && id.Name == "make" && len(v.Args) > 0 {
				_, ok := v.Args[0].(*ast.MapType)
				return ok
			}
		}
		return false
	}
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			switch v := n.(type) {
			case *ast.Field:
				if isMap(v.Type) {
					for _, id := range v.Names {
						names[id.Name] = true
					}
				}
			case *ast.ValueSpec:
				for i, id := range v.Names {
					if (v.Type != nil && isMap(v.Type)) || (i < len(v.Values) && isMap(v.Values[i])) {
						names[id.Name] = true
					}
				}
			case *ast.AssignStmt:
				for i, lhs := range v.Lhs {
					if id, ok := lhs.(*ast.Ident); ok && i < len(v.Rhs) && isMap(v.Rhs[i]) {
						names[id.Name] = true
					}
				}
			}
			return true
		})
	}
	return names
}

func TestWorkflowCodeIsDeterministic(t *testing.T) {
	fset := token.NewFileSet()
	var files []*ast.File
	for _, path := range workflowFiles(t) {
		src, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		f, err := parser.ParseFile(fset, path, src, parser.ParseComments)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, f)
	}
	for _, p := range lint(fset, files) {
		t.Error(p)
	}
}

// lint 返回违反确定性规则的位置与说明，按位置排序。
func lint(fset *token.FileSet, files []*ast.File) []string {
	maps := mapNames(files)
	getters := protoMapGetters()
	var problems []string
	report := func(pos token.Pos, msg string) {
		problems = append(problems, fset.Position(pos).String()+": "+msg)
	}

	for _, f := range files {
		timeAlias := ""
		for _, imp := range f.Imports {
			path, _ := strconv.Unquote(imp.Path.Value)
			if why, bad := forbiddenImports[path]; bad {
				report(imp.Pos(), "工作流代码不能导入 "+path+"："+why)
			}
			if path == "time" {
				timeAlias = "time"
				if imp.Name != nil {
					timeAlias = imp.Name.Name
				}
			}
		}
		var fn string
		ast.Inspect(f, func(n ast.Node) bool {
			switch v := n.(type) {
			case *ast.FuncDecl:
				fn = v.Name.Name
			case *ast.GoStmt:
				report(v.Pos(), "不用原生 goroutine，改用 workflow.Go")
			case *ast.SelectStmt:
				report(v.Pos(), "不用 select，改用 workflow.Selector")
			case *ast.ChanType:
				report(v.Pos(), "不用原生 channel，改用 workflow.Channel")
			case *ast.SendStmt:
				report(v.Pos(), "不用原生 channel 发送")
			case *ast.UnaryExpr:
				if v.Op == token.ARROW {
					report(v.Pos(), "不用原生 channel 接收")
				}
			case *ast.SelectorExpr:
				if pkg, ok := v.X.(*ast.Ident); ok && timeAlias != "" && pkg.Name == timeAlias && forbiddenTimeFuncs[v.Sel.Name] {
					report(v.Pos(), "不用 time."+v.Sel.Name+"，改用 workflow.Now、workflow.Sleep、workflow.NewTimer")
				}
			case *ast.RangeStmt:
				if strings.HasPrefix(fn, "sorted") {
					return true
				}
				switch x := v.X.(type) {
				case *ast.Ident:
					if maps[x.Name] {
						report(v.Pos(), "遍历 map "+x.Name+" 的顺序不确定，先取出键排序")
					}
				case *ast.SelectorExpr:
					if maps[x.Sel.Name] {
						report(v.Pos(), "遍历 map "+x.Sel.Name+" 的顺序不确定，先取出键排序")
					}
				case *ast.CallExpr:
					if sel, ok := x.Fun.(*ast.SelectorExpr); ok && getters[sel.Sel.Name] {
						report(v.Pos(), "遍历 proto map "+sel.Sel.Name+"() 的顺序不确定，先取出键排序")
					}
				}
			}
			return true
		})
	}
	sort.Strings(problems)
	return problems
}

// TestDeterminismLintCatchesViolations 证明上面的检查确实生效：每类违规都要被找出来，
// 而放在 sorted 开头的辅助函数里遍历 map 是允许的。
func TestDeterminismLintCatchesViolations(t *testing.T) {
	src := `package x
import (
	"math/rand"
	"time"
	wf "example.com/workflowv1"
)
type s struct{ m map[string]int }
func f(v *s, in *wf.SessionWorkflowInput) {
	_ = time.Now()
	go func() {}()
	ch := make(chan int)
	select { case <-ch: }
	for k := range v.m { _ = k }
	for k := range in.GetSubmitted() { _ = k }
	_ = rand.Int()
}
func sortedKeys(v *s) []string {
	var keys []string
	for k := range v.m { keys = append(keys, k) }
	return keys
}`
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "x.go", src, 0)
	if err != nil {
		t.Fatal(err)
	}
	problems := strings.Join(lint(fset, []*ast.File{f}), "\n")
	for _, want := range []string{"math/rand", "time.Now", "workflow.Go", "workflow.Selector", "workflow.Channel", "遍历 map m", "GetSubmitted"} {
		if !strings.Contains(problems, want) {
			t.Errorf("检查没有发现“%s”类违规，实际报告：\n%s", want, problems)
		}
	}
	if strings.Contains(problems, "x.go:19") {
		t.Errorf("sorted 开头的辅助函数里遍历 map 不应报告：\n%s", problems)
	}
}
