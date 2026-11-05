// Package envbuild 是按项目定制的运行环境（设计书改动稿 W6 第 3 条；docs/plan/cloud-deploy.md D-17；docs/plan/sandbox-platform.md
// 第 5 节）。SBX-01 从 Worker 的 agentruntime_worker.sandbox.environments、env_builder 与 py-core 的 oci_registry 搬来，行为不变：
//
// 仓库自带环境定义时，第一次用到就构建一个镜像，按“定义的哈希 + 基础镜像 digest”缓存，之后直接用；构建好之前、构建失败、
// 超了个数上限时退回 universal，并在 shell_run 的结果里说明原因（不悄悄忽略）。
//
//   - 识别（Detect）：按顺序找 .devcontainer/devcontainer.json、.devcontainer.json、.tool-versions、根目录的 Dockerfile，用第一个。
//     devcontainer.json（允许注释与结尾逗号）：image（FROM 它）或 build.dockerfile + build.context；features、dockerComposeFile
//     还不支持。.tool-versions：python、nodejs、golang、rust、java（temurin-<版本>+<构建号>）写死的版本，在 universal 之上把对应的
//     工具链换成官方镜像里的那个版本（各占一层），不在构建里现下载工具链。Dockerfile：照原样构建，上下文是仓库根目录。
//   - 静态检查（CheckDockerfile）：用户的 Dockerfile 不可信。FROM、COPY --from、RUN --mount=from 引用的镜像不能在本部署的环境
//     镜像仓库里别的租户的路径下，不能指向地址或内网名字；RUN 的 --network=host、--security=insecure、secret 与 ssh 挂载不收；
//     # syntax 只认 docker/dockerfile；ADD 的远端地址不能是地址或内网名字。真正的隔离靠构建在 microVM 里（K8sJobBuilder）。
//   - 缓存（Store，业务库的 sandbox_images，迁移 204，租户作用域）：building → ready，或 failed（原因）；长期不用的（idle_days）
//     清掉：删仓库里的清单、记为 deleted。个数配额在建行的事务里核对。
//   - 构建（Manager.RunBuild）：在服务进程里跑（原来是 Worker 的 Temporal 构建工作流）。构建期间每分钟在这一行的 last_used_at
//     记一次心跳；服务重启或换了副本，心跳停了几分钟的 building 由下一次用到它的 Acquire 接手重来。构建器把产物（OCI 打包）
//     交到服务的临时文件里，服务按大小上限核对，推到镜像仓库里本租户的路径（<prefix>/<租户>/env:<键>）。仓库的凭据只在服务里。
package envbuild

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"
)

// MaxDefinitionBytes 是环境定义（与它引用的 Dockerfile）的大小上限。
const MaxDefinitionBytes = 64 * 1024

// DefinitionPaths 是按顺序找的定义。
var DefinitionPaths = []string{".devcontainer/devcontainer.json", ".devcontainer.json", ".tool-versions", "Dockerfile"}

// Reader 读工作区里一个文件（相对仓库根的路径）；没有时 (nil, nil)。
type Reader func(path string) ([]byte, error)

// Definition 是仓库里的环境定义。Problem 非空表示认出来了但不支持（退回 universal 的原因）。
type Definition struct {
	Kind    string // devcontainer、tool-versions、dockerfile、setup
	Path    string
	Content []byte
	// Dockerfile 是生成或引用的 Dockerfile。
	Dockerfile string
	// Context 是构建上下文（相对仓库根的目录；"" 表示不需要仓库里的文件）。
	Context string
	Problem string
}

// Key 是缓存键：定义（含生成或引用的 Dockerfile）的哈希加上基础镜像的 digest。与原来 Python 的写法逐字节相同（json.dumps 的
// sort_keys 与默认分隔符），上线前记下的镜像照常命中。
func (d Definition) Key(baseDigest string) string {
	content := sha256.Sum256(d.Content)
	dockerfile := sha256.Sum256([]byte(d.Dockerfile))
	doc := map[string]string{
		"kind": d.Kind, "path": d.Path, "content": hex.EncodeToString(content[:]), "dockerfile": hex.EncodeToString(dockerfile[:]),
		"context": d.Context, "base": baseDigest,
	}
	keys := make([]string, 0, len(doc))
	for k := range doc {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString("{")
	for i, k := range keys {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(pyJSONString(k) + ": " + pyJSONString(doc[k]))
	}
	b.WriteString("}")
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// pyJSONString 同 Python 的 json.dumps(str)（ensure_ascii：非 ASCII 写成 \uXXXX）。
func pyJSONString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r < 0x20:
			fmt.Fprintf(&b, `\u%04x`, r)
		case r < 0x80:
			b.WriteRune(r)
		case r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			r -= 0x10000
			fmt.Fprintf(&b, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
		}
	}
	b.WriteByte('"')
	return b.String()
}

// decodeReplace 同 Python 的 bytes.decode("utf-8", errors="replace")（无效的字节换成 U+FFFD）。
func decodeReplace(data []byte) string {
	if utf8.Valid(data) {
		return string(data)
	}
	var b strings.Builder
	for len(data) > 0 {
		r, n := utf8.DecodeRune(data)
		if r == utf8.RuneError && n <= 1 {
			b.WriteRune('�')
			data = data[1:]
			continue
		}
		b.WriteRune(r)
		data = data[n:]
	}
	return b.String()
}

// Detect 找仓库里的环境定义；没有时 nil。
func Detect(read Reader, baseImage string) (*Definition, error) {
	for _, path := range DefinitionPaths {
		data, err := read(path)
		if err != nil {
			return nil, err
		}
		if data == nil {
			continue
		}
		if len(data) > MaxDefinitionBytes {
			return &Definition{Kind: kindOf(path), Path: path, Content: data[:1024], Problem: fmt.Sprintf("%s 太大（上限 %d 字节）", path, MaxDefinitionBytes)}, nil
		}
		if strings.HasSuffix(path, "devcontainer.json") {
			return devcontainer(read, path, data)
		}
		if path == ".tool-versions" {
			dockerfile, problem := ToolVersionsDockerfile(decodeReplace(data), baseImage)
			return &Definition{Kind: "tool-versions", Path: path, Content: data, Dockerfile: dockerfile, Problem: problem}, nil
		}
		return &Definition{Kind: "dockerfile", Path: path, Content: data, Dockerfile: decodeReplace(data), Context: "."}, nil
	}
	return nil, nil
}

func kindOf(path string) string {
	switch {
	case strings.HasSuffix(path, "devcontainer.json"):
		return "devcontainer"
	case path == ".tool-versions":
		return "tool-versions"
	}
	return "dockerfile"
}

// StripJSONC 去掉 JSON 里的 // 与 /* */ 注释和结尾的逗号（devcontainer.json 允许它们），字符串里的不动。
func StripJSONC(text string) string {
	var out strings.Builder
	inStr := false
	for i := 0; i < len(text); {
		c := text[i]
		if inStr {
			out.WriteByte(c)
			if c == '\\' && i+1 < len(text) {
				out.WriteByte(text[i+1])
				i += 2
				continue
			}
			if c == '"' {
				inStr = false
			}
			i++
			continue
		}
		switch {
		case c == '"':
			inStr = true
			out.WriteByte(c)
			i++
		case strings.HasPrefix(text[i:], "//"):
			for i < len(text) && text[i] != '\n' {
				i++
			}
		case strings.HasPrefix(text[i:], "/*"):
			end := strings.Index(text[i+2:], "*/")
			if end < 0 {
				i = len(text)
			} else {
				i += 2 + end + 2
			}
		default:
			out.WriteByte(c)
			i++
		}
	}
	return trailingComma.ReplaceAllString(out.String(), "$1")
}

var trailingComma = regexp.MustCompile(`,(\s*[}\]])`)

// WithinRepo 把 base（目录）下的相对路径规范化成相对仓库根的路径；出了仓库时 ok 为 false。
func WithinRepo(base, rel string) (string, bool) {
	var parts []string
	for _, p := range strings.Split(base, "/") {
		if p != "" && p != "." {
			parts = append(parts, p)
		}
	}
	if strings.HasPrefix(rel, "/") {
		return "", false
	}
	for _, p := range strings.Split(rel, "/") {
		switch {
		case p == "" || p == ".":
		case p == "..":
			if len(parts) == 0 {
				return "", false
			}
			parts = parts[:len(parts)-1]
		case strings.Contains(p, `\`):
			return "", false
		default:
			parts = append(parts, p)
		}
	}
	if len(parts) == 0 {
		return ".", true
	}
	return strings.Join(parts, "/"), true
}

// truthy 同 Python 的真值（devcontainer.json 里的字段）。
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case float64:
		return x != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

func pyStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	data, _ := json.Marshal(v)
	return string(data)
}

var argName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

func devcontainer(read Reader, path string, data []byte) (*Definition, error) {
	unsupported := func(problem string) (*Definition, error) {
		return &Definition{Kind: "devcontainer", Path: path, Content: data, Problem: problem}, nil
	}
	if !utf8.Valid(data) {
		return unsupported(path + " 不是合法的 JSON")
	}
	var raw any
	if err := json.Unmarshal([]byte(StripJSONC(string(data))), &raw); err != nil {
		return unsupported(path + " 不是合法的 JSON")
	}
	doc, ok := raw.(map[string]any)
	if !ok {
		return unsupported(path + " 要是一个对象")
	}
	if truthy(doc["features"]) {
		return unsupported("devcontainer 的 features 还不支持（要在构建里现下载安装脚本）")
	}
	if truthy(doc["dockerComposeFile"]) {
		return unsupported("devcontainer 的 dockerComposeFile 还不支持")
	}
	baseDir := "."
	if i := strings.LastIndex(path, "/"); i >= 0 {
		baseDir = path[:i]
	}
	build := map[string]any{}
	buildIsDict := true
	if truthy(doc["build"]) {
		build, buildIsDict = doc["build"].(map[string]any)
	}
	if buildIsDict && (truthy(build["dockerfile"]) || truthy(doc["dockerFile"])) {
		relValue := build["dockerfile"]
		if !truthy(relValue) {
			relValue = doc["dockerFile"]
		}
		contextValue := any(".")
		if truthy(build["context"]) {
			contextValue = build["context"]
		}
		dockerfilePath, ok1 := WithinRepo(baseDir, pyStr(relValue))
		context, ok2 := WithinRepo(baseDir, pyStr(contextValue))
		if !ok1 || !ok2 {
			return unsupported("devcontainer 的 dockerfile 或 context 出了仓库")
		}
		text, err := read(dockerfilePath)
		if err != nil {
			return nil, err
		}
		if text == nil {
			return unsupported(fmt.Sprintf("devcontainer 引用的 %s 不存在", dockerfilePath))
		}
		args := map[string]any{}
		if truthy(build["args"]) {
			m, ok := build["args"].(map[string]any)
			if !ok {
				return unsupported("devcontainer 的 build.args 要是字符串")
			}
			args = m
		}
		for _, v := range args {
			if _, ok := v.(string); !ok {
				return unsupported("devcontainer 的 build.args 要是字符串")
			}
		}
		dockerfile := decodeReplace(text)
		if len(args) > 0 {
			// 构建参数写成 Dockerfile 开头的 ARG 默认值（构建器不另传参数，键里就包含了它们）。
			names := make([]string, 0, len(args))
			for k := range args {
				names = append(names, k)
			}
			sort.Strings(names)
			var prelude strings.Builder
			for _, k := range names {
				if argName.MatchString(k) {
					prelude.WriteString("ARG " + k + "=" + pyJSONString(args[k].(string)) + "\n")
				}
			}
			dockerfile = insertArgs(dockerfile, prelude.String())
		}
		return &Definition{Kind: "devcontainer", Path: path, Content: data, Dockerfile: dockerfile, Context: context}, nil
	}
	if image, ok := doc["image"].(string); ok && strings.TrimSpace(image) != "" {
		return &Definition{Kind: "devcontainer", Path: path, Content: data, Dockerfile: "FROM " + strings.TrimSpace(image) + "\nENV HOME=/tmp\n"}, nil
	}
	return unsupported("devcontainer.json 里既没有 image 也没有 build.dockerfile")
}

var parserDirective = regexp.MustCompile(`^\s*#\s*[a-z]+\s*=`)

// insertArgs：ARG 要在第一个 FROM 之前才能给 FROM 用；# syntax 之类的解析指令要留在最前面。
func insertArgs(dockerfile, prelude string) string {
	lines := splitLinesKeepEnds(dockerfile)
	i := 0
	for i < len(lines) && parserDirective.MatchString(lines[i]) {
		i++
	}
	return strings.Join(lines[:i], "") + prelude + strings.Join(lines[i:], "")
}

// splitLinesKeepEnds 同 Python 的 str.splitlines(keepends=True)（只认 \n、\r\n 与 \r）。
func splitLinesKeepEnds(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '\n':
			out = append(out, s[start:i+1])
			start = i + 1
		case '\r':
			if i+1 < len(s) && s[i+1] == '\n' {
				i++
			}
			out = append(out, s[start:i+1])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

// splitLines 同 Python 的 str.splitlines()（不带行尾）。
func splitLines(s string) []string {
	lines := splitLinesKeepEnds(s)
	for i, l := range lines {
		lines[i] = strings.TrimRight(strings.TrimSuffix(strings.TrimSuffix(l, "\n"), "\r"), "\r")
	}
	return lines
}

var (
	semver  = regexp.MustCompile(`^\d+\.\d+\.\d+$`)
	temurin = regexp.MustCompile(`^temurin-(\d+\.\d+\.\d+)\+(\d+)(?:\.\d+)*(?:\.LTS)?$`)
)

const passthrough = "PIP_CACHE_DIR PIP_DISABLE_PIP_VERSION_CHECK npm_config_cache npm_config_update_notifier npm_config_fund GOPATH GOCACHE GOMODCACHE " +
	"GOTOOLCHAIN GOFLAGS JAVA_HOME MAVEN_HOME MAVEN_ARGS RUSTUP_HOME CARGO_HOME"

func firstRunes(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		r = r[:n]
	}
	return string(r)
}

// ToolVersionsDockerfile 按 .tool-versions 生成 Dockerfile；返回（Dockerfile，不支持的原因）。
func ToolVersionsDockerfile(text, baseImage string) (string, string) {
	tools := map[string]string{}
	for _, raw := range splitLines(text) {
		line := raw
		if i := strings.Index(line, "#"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) < 2 {
			return "", ".tool-versions 的这一行看不懂：" + firstRunes(strings.TrimSpace(raw), 80)
		}
		name, version := strings.ToLower(parts[0]), parts[1]
		switch name {
		case "node":
			name = "nodejs"
		case "go":
			name = "golang"
		}
		if _, dup := tools[name]; dup {
			return "", fmt.Sprintf(".tool-versions 里 %s 写了两次", name)
		}
		tools[name] = version
	}
	if len(tools) == 0 {
		return "", ".tool-versions 是空的"
	}
	names := make([]string, 0, len(tools))
	for k := range tools {
		names = append(names, k)
	}
	sort.Strings(names)
	var stages, steps, env []string
	pass := passthrough
	for _, name := range names {
		version := tools[name]
		if name == "java" {
			m := temurin.FindStringSubmatch(version)
			if m == nil {
				return "", "java 要写成 temurin-<版本>+<构建号>（例如 temurin-21.0.5+11.0.LTS），实际 " + version
			}
			stages = append(stages, fmt.Sprintf("FROM eclipse-temurin:%s_%s-jdk-jammy AS java", m[1], m[2]))
			steps = append(steps, "RUN rm -rf /opt/java/openjdk", "COPY --from=java /opt/java/openjdk /opt/java/openjdk")
			continue
		}
		if !semver.MatchString(version) {
			return "", fmt.Sprintf("%s 的版本要写死成三段（例如 1.2.3），实际 %s", name, version)
		}
		switch name {
		case "python":
			stages = append(stages, "FROM python:"+version+"-slim-bookworm AS python")
			steps = append(steps, "COPY --from=python /usr/local /opt/python")
			env = append(env, "PATH=/opt/python/bin:$PATH", "LD_LIBRARY_PATH=/opt/python/lib")
			pass += " LD_LIBRARY_PATH"
		case "nodejs":
			stages = append(stages, "FROM node:"+version+"-bookworm-slim AS nodejs")
			steps = append(steps, "RUN rm -rf /usr/local/lib/node_modules", "COPY --from=nodejs /usr/local/bin/node /usr/local/bin/node",
				"COPY --from=nodejs /usr/local/lib/node_modules /usr/local/lib/node_modules")
		case "golang":
			stages = append(stages, "FROM golang:"+version+"-bookworm AS golang")
			steps = append(steps, "RUN rm -rf /usr/local/go", "COPY --from=golang /usr/local/go /usr/local/go")
		case "rust":
			stages = append(stages, "FROM rust:"+version+"-slim-bookworm AS rust")
			steps = append(steps, "RUN rm -rf /usr/local/rustup /usr/local/cargo", "COPY --from=rust /usr/local/rustup /usr/local/rustup",
				"COPY --from=rust /usr/local/cargo /usr/local/cargo")
		default:
			return "", fmt.Sprintf(".tool-versions 里的 %s 还不支持（支持 python、nodejs、golang、rust、java）", name)
		}
	}
	lines := []string{"# syntax=docker/dockerfile:1.7", "# 由 Runtime 按 .tool-versions 生成（D-17）：在 universal 之上换成写死的版本，各占一层。"}
	lines = append(lines, stages...)
	lines = append(lines, "FROM "+baseImage, "USER 0:0")
	lines = append(lines, steps...)
	if len(env) > 0 {
		lines = append(lines, "ENV "+strings.Join(env, " "))
	}
	lines = append(lines, `ENV AGENT_ENV_PASSTHROUGH="`+pass+`"`, "USER 10001:10001", "")
	return strings.Join(lines, "\n"), ""
}

// SetupDockerfile 是 setup 缓存的环境镜像（ENV-02）：基础镜像（按 digest 固定）加上跑这段脚本；脚本以 base64 写进去，不经构建上下文。
func SetupDockerfile(base, script string) string {
	encoded := base64Std(script)
	return "FROM " + base + "\nWORKDIR /tmp/agentruntime-setup\n" +
		"RUN echo " + encoded + " | base64 -d > /tmp/agentruntime-setup.sh && bash /tmp/agentruntime-setup.sh && rm -f /tmp/agentruntime-setup.sh\n"
}
