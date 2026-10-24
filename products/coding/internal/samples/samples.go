// Package samples 是新建项目时可选的示例仓库（内嵌在二进制里）。
package samples

import (
	"embed"
	"io/fs"
	"os"
	"path/filepath"
)

//go:embed ci_rounding
var files embed.FS

// Sample 是一个示例仓库。
type Sample struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Prompt      string `json:"prompt"`
}

// All 列出全部示例。
var All = []Sample{
	{
		ID:          "ci_rounding",
		Name:        "CI 修复示例（Python）",
		Description: "金额“半分进一”误用了 round()（银行家舍入），五条 unittest 里三条失败。",
		Prompt:      "CI 挂了：在仓库根目录运行 `python -m unittest` 有用例失败。请找出原因并修好 pricing.py，让全部测试通过；不要改测试文件 test_pricing.py。改完再运行一遍 `python -m unittest` 确认。",
	},
}

// Find 按 id 找示例。
func Find(id string) (Sample, bool) {
	for _, s := range All {
		if s.ID == id {
			return s, true
		}
	}
	return Sample{}, false
}

// Write 把示例的文件写进 dir。
func Write(id, dir string) error {
	root, err := fs.Sub(files, id)
	if err != nil {
		return err
	}
	return fs.WalkDir(root, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || path == "." {
			return err
		}
		target := filepath.Join(dir, filepath.FromSlash(path))
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		data, err := fs.ReadFile(root, path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o644)
	})
}
