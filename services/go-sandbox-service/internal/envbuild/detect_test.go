package envbuild

// D-17 的识别、生成与静态检查（从 py-worker 的 test_sandbox_environments.py 搬来）。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const (
	base         = "ghcr.io/agentruntime/sandbox-universal:0.1.0"
	baseDigest   = "sha256:abababababababababababababababababababababababababababababababab"
	toolVersions = "python 3.12.7\nnodejs 20.18.0  # LTS\ngolang 1.23.2\n"
)

var testRegistry = RegistryPath{Host: "127.0.0.1:30500", Prefix: "envs"}

func files(m map[string]string) Reader {
	return func(path string) ([]byte, error) {
		if v, ok := m[path]; ok {
			return []byte(v), nil
		}
		return nil, nil
	}
}

func mustDetect(t *testing.T, read Reader) *Definition {
	t.Helper()
	d, err := Detect(read, base)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestDetectionOrderAndDevcontainerForms(t *testing.T) {
	ws := map[string]string{".tool-versions": toolVersions, "Dockerfile": "FROM python:3.13-slim\n"}
	if d := mustDetect(t, files(ws)); d == nil || d.Kind != "tool-versions" || d.Problem != "" {
		t.Fatalf("tool-versions 先于 Dockerfile：%+v", d)
	}
	ws[".devcontainer/devcontainer.json"] = `{
      // 开发环境
      "build": {"dockerfile": "../Dockerfile", "context": "..", "args": {"VARIANT": "3.13"},},
      /* 结尾逗号与注释都允许 */
      "name": "has // in a string",
    }`
	ws["Dockerfile"] = "# syntax=docker/dockerfile:1.7\nFROM python:${VARIANT}-slim\n"
	d := mustDetect(t, files(ws))
	if d == nil || d.Kind != "devcontainer" || d.Context != "." || d.Problem != "" {
		t.Fatalf("devcontainer：%+v", d)
	}
	if !strings.HasPrefix(d.Dockerfile, "# syntax=docker/dockerfile:1.7\nARG VARIANT=\"3.13\"\nFROM") {
		t.Fatalf("ARG 放在解析指令之后、FROM 之前：%q", d.Dockerfile)
	}
	var got any
	if err := json.Unmarshal([]byte(StripJSONC(`{"a": "x // y", /* c */ "b": [1, 2,],}`)), &got); err != nil ||
		!reflect.DeepEqual(got, map[string]any{"a": "x // y", "b": []any{1.0, 2.0}}) {
		t.Fatalf("StripJSONC：%v %v", got, err)
	}
	for doc, problem := range map[string]string{
		`{"image": "mcr.microsoft.com/devcontainers/python:3.12", "features": {"ghcr.io/devcontainers/features/node:1": {}}}`: "features",
		`{"dockerComposeFile": "compose.yml"}`:                 "dockerComposeFile",
		`{"build": {"dockerfile": "../../../etc/Dockerfile"}}`: "出了仓库",
		`{"name": "nothing"}`:                                  "既没有 image",
		`{"build": {"dockerfile": "missing.Dockerfile"}}`:      "不存在",
		`[1, 2]`: "要是一个对象",
		`{"build": {"dockerfile": "../Dockerfile", "args": {"A": 1}}}`: "build.args",
	} {
		ws[".devcontainer/devcontainer.json"] = doc
		if d := mustDetect(t, files(ws)); d == nil || !strings.Contains(d.Problem, problem) {
			t.Errorf("%s：%+v", doc, d)
		}
	}
	ws[".devcontainer/devcontainer.json"] = `{"image": "mcr.microsoft.com/devcontainers/python:3.12"}`
	if d := mustDetect(t, files(ws)); d == nil || !strings.HasPrefix(d.Dockerfile, "FROM mcr.microsoft.com/devcontainers/python:3.12\n") {
		t.Fatalf("image：%+v", d)
	}
	big := map[string]string{"Dockerfile": strings.Repeat("x", MaxDefinitionBytes+1)}
	if d := mustDetect(t, files(big)); d == nil || !strings.Contains(d.Problem, "太大") || len(d.Content) != 1024 {
		t.Fatalf("太大：%+v", d)
	}
	if d := mustDetect(t, files(map[string]string{})); d != nil {
		t.Fatalf("没有定义：%+v", d)
	}
}

func TestToolVersionsBecomePinnedLayersOnUniversal(t *testing.T) {
	pinned := "ghcr.io/agentruntime/sandbox-universal@" + baseDigest
	dockerfile, problem := ToolVersionsDockerfile(toolVersions+"rust 1.82.0\njava temurin-21.0.5+11.0.LTS\n", pinned)
	if problem != "" {
		t.Fatal(problem)
	}
	lines := strings.Split(dockerfile, "\n")
	for _, want := range []string{"FROM python:3.12.7-slim-bookworm AS python", "FROM node:20.18.0-bookworm-slim AS nodejs", "FROM golang:1.23.2-bookworm AS golang",
		"FROM rust:1.82.0-slim-bookworm AS rust", "FROM eclipse-temurin:21.0.5_11-jdk-jammy AS java", "FROM " + pinned, "USER 10001:10001"} {
		found := false
		for _, l := range lines {
			found = found || l == want
		}
		if !found {
			t.Errorf("少了 %q", want)
		}
	}
	if _, after, _ := strings.Cut(dockerfile, "AGENT_ENV_PASSTHROUGH="); !strings.Contains(after, "LD_LIBRARY_PATH") {
		t.Error("sandbox-agent 要把 Python 的库路径交给命令")
	}
	if p := CheckDockerfile(dockerfile, "t1", testRegistry); len(p) != 0 {
		t.Fatalf("生成的 Dockerfile 要过检查：%v", p)
	}
	for text, problem := range map[string]string{"nodejs lts\n": "三段", "ruby 3.3.0\n": "还不支持", "": "空的", "python 3.12.7\npython 3.13.0\n": "两次",
		"java 21\n": "temurin", "python\n": "看不懂"} {
		if _, p := ToolVersionsDockerfile(text, pinned); !strings.Contains(p, problem) {
			t.Errorf("%q：%q", text, p)
		}
	}
}

func TestMaliciousDockerfilesAreRefused(t *testing.T) {
	cases := map[string]string{
		"FROM 127.0.0.1:30500/envs/t2/env@sha256:" + strings.Repeat("0", 64):          "别的路径",
		"FROM 127.0.0.1:30500/envs/t2/env:latest":                                     "别的路径",
		"FROM 10.43.0.10:5000/internal/base:1":                                        "内网",
		"FROM localhost:5000/base":                                                    "内网",
		"FROM registry.agent-runtime.svc.cluster.local:5000/x":                        "内网",
		"FROM python:3.13\nCOPY --from=127.0.0.1:30500/envs/t2/env:x /secret /":       "别的路径",
		"FROM python:3.13\nRUN --network=host curl http://169.254.169.254/":           "network=host",
		"FROM python:3.13\nRUN --security=insecure mount /dev/sda1 /mnt":              "insecure",
		"FROM python:3.13\nRUN --mount=type=secret,id=aws cat /run/secrets/aws":       "secret",
		"FROM python:3.13\nRUN --mount=type=ssh git clone git@github.com:x/y":         "ssh",
		"FROM python:3.13\nRUN --mount=type=bind,from=10.0.0.5:5000/x,target=/x true": "内网",
		"FROM python:3.13\nADD http://169.254.169.254/latest/meta-data/ /m":           "ADD",
		"FROM python:3.13\nADD http://2130706433/x /x":                                "ADD",
		"FROM python:3.13\nADD http://user:pw@[::1]:80/x /x":                          "ADD",
		"FROM python:3.13\nRUN echo a \\\n  --network=host":                           "network=host",
		"# syntax=evil.example.com/frontend:1\nFROM python:3.13":                      "syntax",
		"FROM ${BASE}": "变量",
		strings.Repeat("#", MaxDefinitionBytes+1): "太大",
	}
	for text, problem := range cases {
		problems := CheckDockerfile(text, "t1", testRegistry)
		ok := false
		for _, p := range problems {
			ok = ok || strings.Contains(p, problem)
		}
		if !ok {
			t.Errorf("%q：%v", text[:min(len(text), 80)], problems)
		}
	}
	own := "FROM 127.0.0.1:30500/envs/t1/env@sha256:" + strings.Repeat("1", 64) + " AS base\nFROM base\nRUN echo ok\nADD https://example.com/x /x\n"
	if p := CheckDockerfile(own, "t1", testRegistry); len(p) != 0 {
		t.Fatalf("自己路径下的镜像与前面的阶段可以用：%v", p)
	}
	if p := CheckDockerfile("FROM python:3.13\n", "t1", RegistryPath{}); len(p) != 0 {
		t.Fatalf("没有登记镜像仓库时只查别的：%v", p)
	}
}

// 缓存键与原来 Python 的写法逐字节相同（上线前 Worker 建好的镜像照常命中）；期望值由 Python 的 json.dumps(sort_keys=True) 算出。
func TestKeysMatchThePythonImplementation(t *testing.T) {
	d := Definition{Kind: "tool-versions", Path: ".tool-versions", Content: []byte("python 3.12.7\n"), Dockerfile: "FROM x\n"}
	if got := d.Key("sha256:" + strings.Repeat("ab", 32)); got != "b63318483a0b049f7710c7eba779ee69cdc25f4880975aeaf6655182eca27efc" {
		t.Fatalf("键：%s", got)
	}
	d = Definition{Kind: "devcontainer", Path: ".devcontainer/devcontainer.json", Content: []byte(`{"name": "é"}`), Dockerfile: "FROM y\n", Context: "sub/目录"}
	if got := d.Key("sha256:" + strings.Repeat("cd", 32)); got != "2d82d753d14aad6717ae886fad996ef8761b2681e7958b3b1db10a422b2a029c" {
		t.Fatalf("非 ASCII 的键：%s", got)
	}
	if got := pyJSONString("ARG 值 \"q\" \\ \t 😀"); got != `"ARG \u503c \"q\" \\ \t \ud83d\ude00"` {
		t.Fatalf("pyJSONString：%s", got)
	}
}

func TestWithinRepo(t *testing.T) {
	for _, c := range []struct {
		base, rel, want string
		ok              bool
	}{
		{".devcontainer", "../Dockerfile", "Dockerfile", true},
		{".devcontainer", "..", ".", true},
		{".", "./a//b/./c", "a/b/c", true},
		{".devcontainer", "../../etc/x", "", false},
		{".", "/etc/passwd", "", false},
		{".", `a\b`, "", false},
	} {
		got, ok := WithinRepo(c.base, c.rel)
		if got != c.want || ok != c.ok {
			t.Errorf("%q %q：%q %v", c.base, c.rel, got, ok)
		}
	}
}

// EV-TB（docs/plan/ev-tb-0of5-2026-11-04.md）：拉官方镜像的层失败时，BuildKit 把镜像仓库 CDN 的签名地址整个打进日志。
// 编出来的、与真的签名同样形状的值（不用线上看到的）。
const (
	signature = "Qm9ndXNTaWduYXR1cmVGb3JUZXN0c0-xYz7~AbCdEfGhIjKlMnOpQrStUv-WxYz0123456789~QwErTyUiOpAsDfGhJkLzXcVbNm-Plo9Okm8Ijn7Uhb"
	layerHex  = "5f1e1b7a2c9d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"
	signedURL = "https://production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/sha256/5f/" + layerHex + "/data" +
		"?Expires=1730383200&Signature=" + signature + "&Key-Pair-Id=K2B7EXAMPLEKEYPAIR"
)

func TestScrubDropsSignedURLsAndOpaqueTokens(t *testing.T) {
	text := "ERROR: failed to do request: Get \"" + signedURL + "\": dial tcp: i/o timeout\n" +
		"retrying https://user:hunter2-password@mirror.example/v2/x?token=abcdef0123456789abcdef0123456789ab\n" +
		"half: " + signature + "~Tr4nc4ted~Fr4gm3ntOfAS1gnedUrl-ZxCvBnMaSdFgHjKlQwErTyUiOp1234&Key-Pair"
	out := Scrub(text)
	for _, leaked := range []string{signature, "K2B7EXAMPLEKEYPAIR", "Expires=1730383200", "hunter2-password", "abcdef0123456789abcdef0123456789ab", "Fr4gm3ntOfAS1gnedUrl"} {
		if strings.Contains(out, leaked) {
			t.Errorf("还有 %s…：%s", leaked[:12], out)
		}
	}
	for _, kept := range []string{"production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/sha256/5f/", layerHex, "i/o timeout", "mirror.example"} {
		if !strings.Contains(out, kept) {
			t.Errorf("要留着 %q：%s", kept, out)
		}
	}
}

// SWE-bench 的环境定义（py-learning 的 benchmarks.swebench.environment_files 生成，testdata/swebench 是它的输出；py-learning 的
// test_swebench 核对两边一致）过得了这里的识别与检查。
func TestSWEBenchDefinitionPasses(t *testing.T) {
	root := filepath.Join("testdata", "swebench")
	read := func(path string) ([]byte, error) {
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(path)))
		if os.IsNotExist(err) {
			return nil, nil
		}
		return data, err
	}
	d := mustDetect(t, read)
	if d == nil || d.Kind != "devcontainer" || d.Problem != "" {
		t.Fatalf("%+v", d)
	}
	if !strings.HasPrefix(d.Dockerfile, "FROM swebench/sweb.eval.x86_64.") {
		t.Fatalf("FROM 官方镜像：%q", d.Dockerfile)
	}
	if p := CheckDockerfile(d.Dockerfile, "bench", RegistryPath{Host: "registry.example", Prefix: "ar"}); len(p) != 0 {
		t.Fatalf("检查：%v", p)
	}
}
