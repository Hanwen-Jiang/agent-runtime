package agent

// docs/plan/running-task-interaction.md 第 2 节验收：调用方断开时命令真的停了。
//
// K8s 与 Cloudflare 后端的立刻打断靠这条：Worker 取消时断开 HTTP 连接（libs/py-core 的 test_sandbox_exec_cancel.py），
// agent 看到断开就杀掉整个进程组，不只是直接的子进程。中间隔着 K8s API Server 或 Cloudflare 的桥时断开不一定传得到，所以调用方另发
// POST /v1/exec/cancel。Windows 上没有进程组（proc_windows.go），这里只看直接的进程。

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// beat 不停地往文件里追加，文件不再变长说明进程停了。Windows 上杀不到子进程，到时间自己退出，不留在机器上。
func beat(path string) {
	for end := time.Now().Add(30 * time.Second); time.Now().Before(end); {
		f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err == nil {
			_, _ = f.WriteString(".")
			_ = f.Close()
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if info, err := os.Stat(path); err == nil && info.Size() > 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("没有等到 %s", path)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func stillGrowing(t *testing.T, path string) bool {
	t.Helper()
	before, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Second)
	after, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return after.Size() > before.Size()
}

func TestAClientThatDisconnectsStopsTheCommandAndItsChildren(t *testing.T) {
	h := newHarness(t, nil)
	marker := filepath.Join(t.TempDir(), "beat")
	// Windows 上杀不到子进程，它会占着测试的临时目录，只跑直接的进程。
	mode := "tree"
	if runtime.GOOS == "windows" {
		mode = "beat"
	}
	body, _ := json.Marshal(execRequest{Argv: h.run(mode, marker), TimeoutMS: (25 * time.Second).Milliseconds()})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.srv.URL+"/v1/exec", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, testToken)
	done := make(chan struct{})
	go func() {
		defer close(done)
		if res, err := http.DefaultClient.Do(req); err == nil {
			_ = res.Body.Close()
		}
	}()
	waitForFile(t, marker)
	if mode == "tree" {
		waitForFile(t, marker+".child")
	}
	cancel()
	<-done
	time.Sleep(200 * time.Millisecond)
	if stillGrowing(t, marker) {
		t.Error("调用方断开了，命令还在跑")
	}
	if mode == "tree" && stillGrowing(t, marker+".child") {
		t.Error("调用方断开了，命令的子进程还在跑")
	}
}

func postCancel(t *testing.T, h *harness, id string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, h.srv.URL+"/v1/exec/cancel", bytes.NewReader([]byte(`{"exec_id":"`+id+`"}`)))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, testToken)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	if res.StatusCode != http.StatusNoContent {
		t.Fatalf("取消应是 204，实际 %d", res.StatusCode)
	}
}

func postExec(t *testing.T, h *harness, r execRequest) int {
	t.Helper()
	body, _ := json.Marshal(r)
	req, err := http.NewRequest(http.MethodPost, h.srv.URL+"/v1/exec", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, testToken)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	return res.StatusCode
}

func TestTheCancelEndpointStopsTheCommand(t *testing.T) {
	h := newHarness(t, nil)
	marker := filepath.Join(t.TempDir(), "beat")
	status := make(chan int, 1)
	go func() {
		status <- postExec(t, h, execRequest{Argv: h.run("beat", marker), TimeoutMS: (25 * time.Second).Milliseconds(), ExecID: "e1"})
	}()
	waitForFile(t, marker)
	postCancel(t, h, "e1")
	select {
	case got := <-status:
		if got != http.StatusConflict {
			t.Errorf("被取消的命令应是 409，实际 %d", got)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("取消之后请求没有结束")
	}
	if stillGrowing(t, marker) {
		t.Error("取消之后命令还在跑")
	}
}

func TestACancelThatArrivesFirstStopsTheCommandFromStarting(t *testing.T) {
	h := newHarness(t, nil)
	marker := filepath.Join(t.TempDir(), "beat")
	postCancel(t, h, "e2")
	if got := postExec(t, h, execRequest{Argv: h.run("beat", marker), TimeoutMS: (25 * time.Second).Milliseconds(), ExecID: "e2"}); got != http.StatusConflict {
		t.Errorf("已经取消的命令应是 409，实际 %d", got)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Error("已经取消的命令不该开始")
	}
}
