// Package pgtest 在测试里拉起临时的 Postgres 实例（计划 S-12a）。
//
// 二进制取自 AR_POSTGRES_BIN 指定的目录，或 PATH 上的 initdb、pg_ctl、psql（见 CLAUDE.md 的常用命令），
// 不让测试自行下载。找不到时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过（与契约测试的约定一致）。
//
// 用法：测试包的 TestMain 调 Main，用例里用 Current(t) 拿到实例，每个用例 NewDatabase 一个新库。
// 管理操作与原始 SQL 都经 psql 执行，测试不依赖 Go 的 Postgres 驱动。
package pgtest

import (
	"bytes"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// AppRole 是在线服务用的数据库角色：能登录，不是超级用户，没有 BYPASSRLS。
const AppRole = "agentruntime_app"

// Cluster 是一个临时实例（trust 认证，只监听 127.0.0.1）。
type Cluster struct {
	bin  string
	dir  string
	port int

	mu      sync.Mutex
	counter int
	roles   map[string]bool
}

var (
	current  *Cluster
	startErr error
)

func tool(bin, name string) (string, error) {
	exe := name
	if runtime.GOOS == "windows" {
		exe += ".exe"
	}
	if bin != "" {
		p := filepath.Join(bin, exe)
		if _, err := os.Stat(p); err != nil {
			return "", fmt.Errorf("AR_POSTGRES_BIN 里没有 %s", exe)
		}
		return p, nil
	}
	return exec.LookPath(name)
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

func run(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err := cmd.Run()
	return out.String(), err
}

// runToFile 把输出写进文件。pg_ctl start 拉起的服务进程会继承输出句柄：用管道接输出时，
// 要等服务进程退出管道才关闭，Run 就一直不返回；交给文件句柄则 pg_ctl 一退出就返回。
func runToFile(path, name string, args ...string) (string, error) {
	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	cmd := exec.Command(name, args...)
	cmd.Stdout, cmd.Stderr = f, f
	err = cmd.Run()
	out, _ := os.ReadFile(path)
	return string(out), err
}

func start() (*Cluster, error) {
	bin := os.Getenv("AR_POSTGRES_BIN")
	initdb, err := tool(bin, "initdb")
	if err != nil {
		return nil, fmt.Errorf("找不到 initdb（Postgres 的二进制），见 CLAUDE.md 的常用命令：%w", err)
	}
	dir, err := os.MkdirTemp("", "agentruntime-pg-")
	if err != nil {
		return nil, err
	}
	port, err := freePort()
	if err != nil {
		return nil, err
	}
	c := &Cluster{bin: filepath.Dir(initdb), dir: dir, port: port, roles: map[string]bool{}}
	data := filepath.Join(dir, "data")
	if out, err := run(initdb, "-D", data, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"); err != nil {
		return nil, fmt.Errorf("initdb 失败：%v\n%s", err, out)
	}
	pgctl, _ := tool(c.bin, "pg_ctl")
	opts := fmt.Sprintf("-p %d -c listen_addresses=127.0.0.1 -c fsync=off -c max_connections=200", port)
	if runtime.GOOS != "windows" {
		// 发行版的默认套接字目录（如 /var/run/postgresql）普通用户写不进；连接都走 TCP，套接字放在临时目录里。
		opts += " -c unix_socket_directories=" + dir
	}
	if out, err := runToFile(filepath.Join(dir, "pg_ctl.out"), pgctl, "-D", data, "-l", filepath.Join(dir, "postgres.log"), "-o", opts, "-w", "start"); err != nil {
		log, _ := os.ReadFile(filepath.Join(dir, "postgres.log"))
		return nil, fmt.Errorf("pg_ctl start 失败：%v\n%s\n%s", err, out, log)
	}
	return c, nil
}

func (c *Cluster) stop() {
	pgctl, _ := tool(c.bin, "pg_ctl")
	_, _ = run(pgctl, "-D", filepath.Join(c.dir, "data"), "-m", "immediate", "-w", "stop")
	_ = os.RemoveAll(c.dir)
}

// Main 在 TestMain 里调用：启动实例，跑完用例后停止并删除。
func Main(m *testing.M) int {
	current, startErr = start()
	code := m.Run()
	if current != nil {
		current.stop()
	}
	return code
}

// Current 返回本测试进程的实例；实例没起来时让用例失败（或按 AR_ALLOW_MISSING_TOOLS 跳过）。
func Current(tb testing.TB) *Cluster {
	tb.Helper()
	if current == nil {
		if startErr == nil {
			startErr = errors.New("没有调用 pgtest.Main")
		}
		if os.Getenv("AR_ALLOW_MISSING_TOOLS") == "1" {
			tb.Skip(startErr.Error())
		}
		tb.Fatal(startErr)
	}
	return current
}

// DSN 返回以 role 身份连接数据库 db 的连接串。
func (c *Cluster) DSN(role, db string) string {
	return fmt.Sprintf("postgres://%s@127.0.0.1:%d/%s?sslmode=disable", role, c.port, db)
}

// SuperuserDSN 返回以超级用户 postgres 连接 db 的连接串。
func (c *Cluster) SuperuserDSN(db string) string { return c.DSN("postgres", db) }

// Psql 以 role 身份在 db 里执行 SQL，返回无对齐、| 分隔的输出（每行一条记录）。
func (c *Cluster) Psql(role, db, sql string) (string, error) {
	psql, err := tool(c.bin, "psql")
	if err != nil {
		return "", err
	}
	out, err := run(psql, "-h", "127.0.0.1", "-p", fmt.Sprint(c.port), "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t", "-F", "|", "-c", sql)
	// Windows 上 psql 按 CRLF 换行。
	return strings.TrimSpace(strings.ReplaceAll(out, "\r\n", "\n")), err
}

// MustPsql 同 Psql，出错时让用例失败。
func (c *Cluster) MustPsql(tb testing.TB, role, db, sql string) string {
	tb.Helper()
	out, err := c.Psql(role, db, sql)
	if err != nil {
		tb.Fatalf("psql 失败：%v\n%s\nSQL：%s", err, out, sql)
	}
	return out
}

// EnsureRole 建一个能登录的角色（attrs 例如 "NOSUPERUSER NOBYPASSRLS"、"BYPASSRLS"），已存在时不变。
func (c *Cluster) EnsureRole(tb testing.TB, name, attrs string) {
	tb.Helper()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.roles[name] {
		return
	}
	c.MustPsql(tb, "postgres", "postgres", fmt.Sprintf("CREATE ROLE %s LOGIN %s", name, attrs))
	c.roles[name] = true
}

// NewDatabase 新建一个属于在线服务角色（AppRole）的库，返回库名。用例结束时删除。
func (c *Cluster) NewDatabase(tb testing.TB) string {
	tb.Helper()
	c.EnsureRole(tb, AppRole, "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE")
	c.mu.Lock()
	c.counter++
	db := fmt.Sprintf("s12a_%d", c.counter)
	c.mu.Unlock()
	c.MustPsql(tb, "postgres", "postgres", fmt.Sprintf("CREATE DATABASE %s OWNER %s", db, AppRole))
	tb.Cleanup(func() {
		_, _ = c.Psql("postgres", "postgres", fmt.Sprintf("DROP DATABASE IF EXISTS %s WITH (FORCE)", db))
	})
	return db
}
