package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// D-17：构建产物经预签名地址上传；超过上限不传，失败时错误里不带地址。
func TestUploadPutsTheFileAndRespectsTheLimit(t *testing.T) {
	var got []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Query().Get("sig") != "s" {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		got, _ = io.ReadAll(r.Body)
	}))
	defer srv.Close()
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, []byte("oci-layout-bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := upload(path, srv.URL+"/obj?sig=s", 1024); err != nil || string(got) != "oci-layout-bytes" {
		t.Fatalf("上传：%v %q", err, got)
	}
	if err := upload(path, srv.URL+"/obj?sig=s", 4); err == nil || !strings.Contains(err.Error(), "超过上限") {
		t.Fatalf("超过上限应当不传：%v", err)
	}
	err := upload(path, srv.URL+"/obj?sig=wrong", 1024)
	if err == nil || strings.Contains(err.Error(), "wrong") || strings.Contains(err.Error(), srv.URL) {
		t.Fatalf("失败时报 HTTP 状态，不带地址：%v", err)
	}
	if err := upload(path, "", 1024); err == nil {
		t.Fatal("没有地址应当失败")
	}
}
