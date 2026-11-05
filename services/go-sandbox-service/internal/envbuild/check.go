package envbuild

import (
	"encoding/base64"
	"fmt"
	"net"
	"regexp"
	"sort"
	"strings"
)

func base64Std(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

var internalSuffixes = []string{".local", ".localhost", ".internal", ".svc", ".cluster.local", ".lan", ".home.arpa"}

var numericHost = regexp.MustCompile(`^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$`)

// internalHost 判断主机是不是地址或内网名字（含十进制、八进制、十六进制写的地址，inet_aton 认的写法）。
func internalHost(host string) bool {
	h := strings.TrimRight(strings.Trim(strings.ToLower(host), "[]"), ".")
	if strings.Count(h, ":") == 1 {
		h = h[:strings.LastIndex(h, ":")]
	}
	if h == "localhost" {
		return true
	}
	for _, s := range internalSuffixes {
		if strings.HasSuffix(h, s) {
			return true
		}
	}
	if net.ParseIP(h) != nil {
		return true
	}
	return numericHost.MatchString(h)
}

// imageHost 是镜像引用里的仓库主机；Docker Hub 的简写（没有主机）时为空。
func imageHost(ref string) string {
	first, _, hasSlash := strings.Cut(ref, "/")
	if hasSlash && (strings.Contains(first, ".") || strings.Contains(first, ":") || first == "localhost") {
		return first
	}
	return ""
}

// RegistryPath 是本部署放构建产物的镜像仓库：Host（拉取用的主机名）与路径前缀；租户的镜像在 <host>/<prefix>/<租户>/env。
type RegistryPath struct {
	Host, Prefix string
}

// Repo 是租户的镜像路径。
func (r RegistryPath) Repo(tenantID string) string {
	if r.Prefix != "" {
		return r.Prefix + "/" + tenantID + "/env"
	}
	return tenantID + "/env"
}

// 构建日志与失败原因里看着像凭据的东西（EV-TB，docs/plan/ev-tb-0of5-2026-11-04.md）：拉镜像层失败时 BuildKit 会打出镜像仓库 CDN
// 的签名地址，签名与密钥对的 ID 在查询串里。URL 的 userinfo 与查询串整个去掉，主机与路径留着；截断后剩下的半截签名这类不透明的
// 长串也去掉（层的 sha256 这类十六进制的 digest 不是凭据，留着）。
var (
	urlUserinfo = regexp.MustCompile(`(https?://)[^/\s"'<>@]+@`)
	urlQuery    = regexp.MustCompile(`(https?://[^\s"'<>?#]+)\?[^\s"'<>]*`)
	opaque      = regexp.MustCompile(`[A-Za-z0-9~_\-+=%]{40,}`)
	hexOnly     = regexp.MustCompile(`^[0-9a-fA-F]+$`)
)

// Scrub 去掉文字里看着像凭据的部分（见上）；给要存下来或给人看的失败原因用。
func Scrub(text string) string {
	text = urlUserinfo.ReplaceAllString(text, "${1}***@")
	text = urlQuery.ReplaceAllString(text, "${1}?***")
	return opaque.ReplaceAllStringFunc(text, func(m string) string {
		if hexOnly.MatchString(m) {
			return m
		}
		return "***"
	})
}

var (
	syntaxDirective = regexp.MustCompile(`(?im)^\s*#\s*syntax\s*=\s*(\S+)`)
	syntaxAllowed   = regexp.MustCompile(`^(docker\.io/)?docker/dockerfile(:[\w.-]+)?(@sha256:[0-9a-f]{64})?$`)
	copyFrom        = regexp.MustCompile(`--from=(\S+)`)
	runNetworkHost  = regexp.MustCompile(`--network=host\b`)
	runInsecure     = regexp.MustCompile(`--security=insecure\b`)
	runMount        = regexp.MustCompile(`--mount=(\S+)`)
	addURL          = regexp.MustCompile(`(https?://\S+)`)
)

// CheckDockerfile 是用户的 Dockerfile 的静态检查；返回不收的原因（排好序、去重；空表示通过）。registry 的 Host 为空时不核对仓库路径。
func CheckDockerfile(text, tenantID string, registry RegistryPath) []string {
	if len(text) > MaxDefinitionBytes {
		return []string{fmt.Sprintf("Dockerfile 太大（上限 %d 字节）", MaxDefinitionBytes)}
	}
	var problems []string
	for _, m := range syntaxDirective.FindAllStringSubmatch(text, -1) {
		if !syntaxAllowed.MatchString(m[1]) {
			problems = append(problems, "# syntax 只认 docker/dockerfile，不收 "+m[1])
		}
	}
	stages := map[string]bool{}
	for _, ins := range instructions(text) {
		switch ins.name {
		case "FROM":
			var words []string
			for _, w := range strings.Fields(ins.args) {
				if !strings.HasPrefix(w, "--") {
					words = append(words, w)
				}
			}
			if len(words) == 0 {
				continue
			}
			problems = append(problems, checkRef(words[0], tenantID, registry, stages)...)
			if len(words) >= 3 && strings.ToLower(words[1]) == "as" {
				stages[strings.ToLower(words[2])] = true
			}
		case "COPY":
			for _, m := range copyFrom.FindAllStringSubmatch(ins.args, -1) {
				problems = append(problems, checkRef(m[1], tenantID, registry, stages)...)
			}
		case "RUN":
			if runNetworkHost.MatchString(ins.args) {
				problems = append(problems, "RUN --network=host 不收")
			}
			if runInsecure.MatchString(ins.args) {
				problems = append(problems, "RUN --security=insecure 不收")
			}
			for _, m := range runMount.FindAllStringSubmatch(ins.args, -1) {
				opts := map[string]string{}
				for _, p := range strings.Split(m[1], ",") {
					k, v, _ := strings.Cut(p, "=")
					opts[k] = v
				}
				if t := opts["type"]; t == "secret" || t == "ssh" {
					problems = append(problems, "RUN --mount=type="+t+" 不收（构建里没有密钥）")
				}
				if from := opts["from"]; from != "" {
					problems = append(problems, checkRef(from, tenantID, registry, stages)...)
				}
			}
		case "ADD":
			for _, m := range addURL.FindAllStringSubmatch(ins.args, -1) {
				if internalHost(urlHostname(m[1])) {
					problems = append(problems, "ADD 的地址 "+firstRunes(m[1], 80)+" 指向地址或内网名字")
				}
			}
		}
	}
	seen := map[string]bool{}
	out := []string{}
	for _, p := range problems {
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out
}

// urlHostname 同 Python 的 urlsplit(url).hostname（宽松：去掉 userinfo 与端口，小写）。
func urlHostname(raw string) string {
	_, rest, ok := strings.Cut(raw, "://")
	if !ok {
		return ""
	}
	if i := strings.IndexAny(rest, "/?#"); i >= 0 {
		rest = rest[:i]
	}
	if i := strings.LastIndex(rest, "@"); i >= 0 {
		rest = rest[i+1:]
	}
	if strings.HasPrefix(rest, "[") {
		if end := strings.Index(rest, "]"); end > 0 {
			return strings.ToLower(rest[1:end])
		}
	}
	if i := strings.LastIndex(rest, ":"); i >= 0 {
		rest = rest[:i]
	}
	return strings.ToLower(rest)
}

func checkRef(ref, tenantID string, registry RegistryPath, stages map[string]bool) []string {
	if strings.Contains(ref, "$") {
		return []string{"镜像引用里不能用变量：" + ref}
	}
	if stages[strings.ToLower(ref)] || ref == "scratch" {
		return nil
	}
	host := imageHost(ref)
	if registry.Host != "" && strings.EqualFold(host, registry.Host) {
		_, path, _ := strings.Cut(ref, "/")
		repo := registry.Repo(tenantID)
		if !strings.HasPrefix(path, repo+"@") && !strings.HasPrefix(path, repo+":") {
			return []string{"不能用环境镜像仓库里别的路径：" + ref}
		}
		return nil
	}
	if host != "" && internalHost(host) {
		return []string{"镜像 " + ref + " 指向地址或内网名字"}
	}
	return nil
}

type instruction struct{ name, args string }

// instructions 把 Dockerfile 拆成（指令，参数）：处理续行与注释。
func instructions(text string) []instruction {
	var out []instruction
	add := func(buf string) {
		buf = strings.TrimSpace(buf)
		if buf == "" {
			return
		}
		name, args := buf, ""
		if i := strings.IndexAny(buf, " \t\v\f\r\n"); i >= 0 {
			name, args = buf[:i], strings.TrimLeft(buf[i:], " \t\v\f\r\n")
		}
		out = append(out, instruction{strings.ToUpper(name), args})
	}
	buf := ""
	for _, raw := range splitLines(text) {
		line := strings.TrimRight(raw, " \t\v\f\r")
		if buf == "" && (strings.TrimSpace(line) == "" || strings.HasPrefix(strings.TrimLeft(line, " \t\v\f"), "#")) {
			continue
		}
		if strings.HasSuffix(line, `\`) {
			buf += line[:len(line)-1] + " "
			continue
		}
		buf += line
		add(buf)
		buf = ""
	}
	add(buf)
	return out
}
