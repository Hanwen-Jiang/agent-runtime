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

// Sample 是一个示例仓库。名字、说明与起手的任务按语言给（zh、en）；仓库里的文件只有一份。
type Sample struct {
	ID   string
	Text map[string]Text
}

// Text 是示例给用户看的文字。
type Text struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Prompt      string `json:"prompt"`
}

// In 是这种语言的文字；没有这种语言时用中文。
func (s Sample) In(lang string) Text {
	if t, ok := s.Text[lang]; ok {
		return t
	}
	return s.Text["zh"]
}

// All 列出全部示例。
var All = []Sample{
	{
		ID: "ci_rounding",
		Text: map[string]Text{
			"zh": {
				Name:        "CI 修复示例（Python）",
				Description: "金额“半分进一”误用了 round()（银行家舍入），五条 unittest 里三条失败。",
				Prompt:      "CI 挂了：在仓库根目录运行 `python -m unittest` 有用例失败。请找出原因并修好 pricing.py，让全部测试通过；不要改测试文件 test_pricing.py。改完再运行一遍 `python -m unittest` 确认。",
			},
			"en": {
				Name:        "CI fix sample (Python)",
				Description: "Half-cent rounding uses round(), which rounds half to even; three of five unittest cases fail.",
				Prompt:      "CI is failing: running `python -m unittest` in the repository root has failing tests. Find the cause and fix pricing.py so that all tests pass; don't change the test file test_pricing.py. Run `python -m unittest` again afterwards to confirm.",
			},
		},
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
