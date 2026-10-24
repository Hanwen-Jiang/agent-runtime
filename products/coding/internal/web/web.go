// Package web 是内嵌的网页（原生 JS 单页应用，没有构建步骤）。
package web

import (
	"embed"
	"io/fs"
	"net/http"
	"strings"
)

//go:embed static
var static embed.FS

// Handler 提供 /assets/ 下的静态文件；其余路径一律回 index.html（单页应用自己处理路由）。
func Handler() http.Handler {
	root, _ := fs.Sub(static, "static")
	files := http.StripPrefix("/assets/", http.FileServer(http.FS(root)))
	index, _ := fs.ReadFile(root, "index.html")
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/assets/") {
			w.Header().Set("Cache-Control", "no-cache")
			files.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		_, _ = w.Write(index)
	})
}
