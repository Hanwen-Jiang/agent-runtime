// Command sandbox-agent 在沙箱环境（K8s 的沙箱 Pod、Cloudflare 的 Container）里跑，替 Runtime 的 Sandbox 适配器执行命令、
// 读写文件。接口见 internal/agent；计划见 docs/plan/cloud-deploy.md 的 D-05、D-07。只用标准库，静态编译，放进任何镜像都能跑。
//
//	sandbox-agent                 起服务
//	sandbox-agent install <路径>  把自己拷到 <路径>：K8s 的 initContainer 用它把 agent 放进共享卷，跑命令的镜像不用自带 agent
//	sandbox-agent upload <文件> <最大字节数>
//	                              把构建出的镜像（OCI 打包）PUT 到环境变量 AGENT_UPLOAD_URL 给的预签名地址（按项目定制的运行环境，
//	                              D-17）：构建在 microVM 里，手里没有镜像仓库的凭据，只有这一个限时、只能写这一个对象的地址；
//	                              超过最大字节数时不传、退出码非零。
//	sandbox-agent version
//
// 环境变量：
//
//	AGENT_TOKEN 或 AGENT_TOKEN_FILE  请求要带的令牌（至少 32 个字符；必填）
//	AGENT_ADDR                       监听地址，默认 :7070
//	AGENT_WORKDIR                    工作目录，默认 /workspace
//	AGENT_MAX_TIMEOUT_S              一条命令最长多少秒，默认 3600
//	AGENT_OUTPUT_LIMIT               stdout、stderr 各自最多保留多少字节，默认 8 MiB
//	AGENT_FILE_LIMIT                 一个文件最多多少字节，默认 64 MiB
//	AGENT_ENV_PASSTHROUGH            另外交给命令的变量名（空格或逗号分隔）：镜像用它声明工具链要的变量（D-14）
//
// 命令只拿到这些环境变量：PATH、HOME（默认 /tmp）、LANG（默认 C.UTF-8）、LC_ALL、TZ（Windows 上另有 SYSTEMROOT、WINDIR、
// PATHEXT），加上 PYTHONIOENCODING=utf-8 与 PYTHONDONTWRITEBYTECODE=1，加上 AGENT_ENV_PASSTHROUGH 列出的；AGENT_* 一个都不给。
// 每次执行还可以带这次的环境变量（请求的 env）。
package main

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/services/go-sandbox-agent/internal/agent"
)

const version = "0.1.0"

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "install":
			if len(os.Args) != 3 {
				log.Fatal("用法：sandbox-agent install <路径>")
			}
			if err := install(os.Args[2]); err != nil {
				log.Fatalf("install：%v", err)
			}
			return
		case "upload":
			if len(os.Args) != 4 {
				log.Fatal("用法：sandbox-agent upload <文件> <最大字节数>（地址在 AGENT_UPLOAD_URL）")
			}
			limit, err := strconv.ParseInt(os.Args[3], 10, 64)
			if err != nil || limit <= 0 {
				log.Fatal("最大字节数要是正整数")
			}
			if err := upload(os.Args[2], os.Getenv("AGENT_UPLOAD_URL"), limit); err != nil {
				log.Fatalf("upload：%v", err)
			}
			return
		case "version":
			fmt.Println(version)
			return
		default:
			log.Fatalf("不认识的子命令 %s", os.Args[1])
		}
	}
	token := os.Getenv("AGENT_TOKEN")
	if file := os.Getenv("AGENT_TOKEN_FILE"); token == "" && file != "" {
		data, err := os.ReadFile(file)
		if err != nil {
			log.Fatalf("读不了 AGENT_TOKEN_FILE：%v", err)
		}
		token = strings.TrimSpace(string(data))
	}
	_ = os.Unsetenv("AGENT_TOKEN")
	handler, err := agent.New(agent.Config{
		Workdir:     envOr("AGENT_WORKDIR", "/workspace"),
		Token:       token,
		Env:         commandEnv(),
		OutputLimit: int(intEnv("AGENT_OUTPUT_LIMIT", 8<<20)),
		FileLimit:   intEnv("AGENT_FILE_LIMIT", 64<<20),
		MaxTimeout:  time.Duration(intEnv("AGENT_MAX_TIMEOUT_S", 3600)) * time.Second,
	})
	if err != nil {
		log.Fatal(err)
	}
	addr := envOr("AGENT_ADDR", ":7070")
	srv := &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	log.Printf("sandbox-agent %s 在 %s 上运行", version, addr)
	log.Fatal(srv.ListenAndServe())
}

func commandEnv() []string {
	env := []string{
		"PATH=" + envOr("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
		"HOME=" + envOr("HOME", "/tmp"),
		"LANG=" + envOr("LANG", "C.UTF-8"),
		"PYTHONIOENCODING=utf-8",
		"PYTHONDONTWRITEBYTECODE=1",
	}
	// SYSTEMROOT、WINDIR、PATHEXT 只在 Windows 上有：本机测试时 Python 没有 SYSTEMROOT 启动不了。
	for _, k := range []string{"LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT"} {
		if v := os.Getenv(k); v != "" {
			env = append(env, k+"="+v)
		}
	}
	return append(env, agent.PassThrough(os.Environ(), os.Getenv("AGENT_ENV_PASSTHROUGH"))...)
}

func install(dest string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	src, err := os.Open(self)
	if err != nil {
		return err
	}
	defer src.Close()
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	out, err := os.OpenFile(dest, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, src); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// upload 把文件 PUT 到预签名地址；超过 limit 字节时不传。地址只在环境变量里，不进日志与错误信息。
func upload(path, url string, limit int64) error {
	if url == "" {
		return fmt.Errorf("没有 AGENT_UPLOAD_URL")
	}
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if info.Size() > limit {
		return fmt.Errorf("镜像有 %d 字节，超过上限 %d", info.Size(), limit)
	}
	req, err := http.NewRequest(http.MethodPut, url, f)
	if err != nil {
		return fmt.Errorf("预签名地址不对")
	}
	req.ContentLength = info.Size()
	req.Header.Set("Content-Type", "application/vnd.oci.image.layout.v1+tar")
	client := &http.Client{Timeout: 30 * time.Minute}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("上传失败（%T）", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("上传失败：HTTP %d", resp.StatusCode)
	}
	return nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func intEnv(key string, fallback int64) int64 {
	if v := os.Getenv(key); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n <= 0 {
			log.Fatalf("%s 要是正整数", key)
		}
		return n
	}
	return fallback
}
