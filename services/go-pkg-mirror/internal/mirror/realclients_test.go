package mirror_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// D-15 验收 1：三种生态的真实客户端（pip、npm、go）经镜像装一个样例包，第二次装（客户端自己的缓存是新的）时上游的包文件
// 流量为 0。上游是本机的假官方源（fake_test.go），不联网。找不到客户端时跳过；设 AR_PKG_MIRROR_REQUIRE_CLIENTS=1 时
// 改为失败（CI 上要求跑）。Python 用 AR_TEST_PYTHON 指定（要带 pip），否则找 PATH 上的 python3、python。

func requireClient(t *testing.T, names ...string) string {
	t.Helper()
	for _, n := range names {
		if p, err := exec.LookPath(n); err == nil {
			return p
		}
	}
	if os.Getenv("AR_PKG_MIRROR_REQUIRE_CLIENTS") == "1" {
		t.Fatalf("找不到 %v", names)
	}
	t.Skipf("找不到 %v，跳过真实客户端的测试", names)
	return ""
}

func run(t *testing.T, dir string, env []string, name string, args ...string) string {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), env...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s %s 失败：%v\n%s", filepath.Base(name), strings.Join(args, " "), err, out)
	}
	return string(out)
}

func TestRealPipInstallsThroughTheMirrorAndTheSecondTimeCostsNoUpstream(t *testing.T) {
	python := os.Getenv("AR_TEST_PYTHON")
	if python == "" {
		python = requireClient(t, "python3", "python")
	}
	if err := exec.Command(python, "-m", "pip", "--version").Run(); err != nil {
		if os.Getenv("AR_PKG_MIRROR_REQUIRE_CLIENTS") == "1" {
			t.Fatalf("%s 没有 pip", python)
		}
		t.Skipf("%s 没有 pip", python)
	}
	f := newFixture(t, nil)
	index := f.http.URL + "/t/" + token(t, "t-pip", 0) + "/pypi/simple/"
	for i := 0; i < 2; i++ {
		target := t.TempDir()
		run(t, target, []string{"PIP_CONFIG_FILE=" + os.DevNull}, python, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--disable-pip-version-check",
			"--target", target, "--index-url", index, "--trusted-host", "127.0.0.1", "demo-pkg==1.0.0")
		out := run(t, target, []string{"PYTHONPATH=" + target}, python, "-c", "import demo_pkg; print(demo_pkg.VALUE)")
		if strings.TrimSpace(out) != "42" {
			t.Fatalf("装上的包不对：%q", out)
		}
	}
	if n := f.up.total("/files/"); n != 1 {
		t.Fatalf("第二次装时上游的包文件流量为 0：上游一共被拉了 %d 次", n)
	}
}

func TestRealNpmInstallsThroughTheMirrorAndTheSecondTimeCostsNoUpstream(t *testing.T) {
	npm := requireClient(t, map[bool]string{true: "npm.cmd", false: "npm"}[runtime.GOOS == "windows"], "npm")
	f := newFixture(t, nil)
	registry := f.http.URL + "/t/" + token(t, "t-npm", 0) + "/npm/"
	for i := 0; i < 2; i++ {
		prefix := t.TempDir()
		run(t, prefix, []string{"npm_config_cache=" + filepath.Join(prefix, ".cache"), "npm_config_userconfig=" + filepath.Join(prefix, ".npmrc")},
			npm, "install", "demo-pkg@1.0.0", "--registry", registry, "--prefix", prefix, "--no-audit", "--no-fund", "--no-package-lock", "--no-save")
		data, err := os.ReadFile(filepath.Join(prefix, "node_modules", "demo-pkg", "index.js"))
		if err != nil || !strings.Contains(string(data), "42") {
			t.Fatalf("装上的包不对：%v", err)
		}
	}
	if n := f.up.hit("/npm/demo-pkg/-/demo-pkg-1.0.0.tgz"); n != 1 {
		t.Fatalf("第二次装时上游的包文件流量为 0：tarball 被拉了 %d 次", n)
	}
}

func TestRealGoDownloadsThroughTheMirrorAndTheSecondTimeCostsNoUpstream(t *testing.T) {
	gocmd := requireClient(t, "go")
	f := newFixture(t, nil)
	proxy := f.http.URL + "/t/" + token(t, "t-go", 0) + "/go"
	for i := 0; i < 2; i++ {
		home := t.TempDir()
		// This disposable module cache must be removable by the non-root test
		// user after checking both real downloads and the upstream cache hit.
		env := []string{"GOPROXY=" + proxy, "GOSUMDB=off", "GOFLAGS=-mod=mod -modcacherw", "GOWORK=off", "GO111MODULE=on", "GONOSUMDB=*", "GONOPROXY=", "GOPRIVATE=",
			"GOPATH=" + filepath.Join(home, "gopath"), "GOMODCACHE=" + filepath.Join(home, "mod"), "GOCACHE=" + filepath.Join(home, "cache"), "GOTOOLCHAIN=local"}
		out := run(t, home, env, gocmd, "mod", "download", "-json", "example.com/demo@v1.0.0")
		if !strings.Contains(out, `"Version": "v1.0.0"`) || strings.Contains(out, `"Error"`) {
			t.Fatalf("go mod download：%s", out)
		}
	}
	if n := f.up.hit("/go/example.com/demo/@v/v1.0.0.zip"); n != 1 {
		t.Fatalf("第二次下载时上游的 zip 流量为 0：被拉了 %d 次", n)
	}
}
