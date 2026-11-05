package server_test

// D-17：按项目定制的运行环境在服务里（从 py-worker 的 test_sandbox_environments.py、test_tuning_env_failures.py 与 test_env02_environment.py
// 的 setup 缓存部分搬来；docs/plan/cloud-deploy.md D-17 的验收）。
//
//  1. 带 .tool-versions 的仓库第一次用时构建、第二次直接用缓存。
//  2. 恶意的 Dockerfile 静态检查拒绝，不起构建；产物只推到本租户的路径；构建期间一行 env-builder、一条平台批准的授权，结束后收回。
//  3. 超过大小上限或超时的构建被终止，环境退回 universal 并说明原因；个数配额；长期不用的镜像清掉；失败过一段时间可以再试。
//  4. 心跳停了的构建（服务重启、换了副本）由下一次用到它的调用接手；构建在服务进程里跑。
//  5. 镜像落不到这次的后端时退回 universal（state=fallback）；setup 成功的结果经环境镜像缓存。

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild/envbuildtest"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

const (
	envBase       = "ghcr.io/agentruntime/sandbox-universal:0.1.0"
	envBaseDigest = "sha256:abababababababababababababababababababababababababababababababab"
	toolVersions  = "python 3.12.7\nnodejs 20.18.0  # LTS\ngolang 1.23.2\n"
	customImages  = `{"default": "local", "backends": {"local": {"kind": "local", "isolation": "none", "custom_images": true}}}`
)

var buildHosts = []string{"registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com"}

// fakeBuilder 不起 Job：按场景把一个 OCI 打包写出来，或报超时、出错。记下拿到的 BuildJob 与构建时的环境表。
type fakeBuilder struct {
	h      *harness
	mu     sync.Mutex
	jobs   []envbuild.BuildJob
	mode   string
	reason string
	during struct {
		envs   [][3]string
		grants [][3]string
	}
}

func (b *fakeBuilder) Build(ctx context.Context, job envbuild.BuildJob, out io.Writer) (envbuild.Outcome, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.jobs = append(b.jobs, job)
	led := b.h.svc.Broker().Ledger()
	active, err := led.Active(ctx, job.Owner)
	if err != nil {
		return envbuild.Outcome{}, err
	}
	b.during.envs, b.during.grants = nil, nil
	for _, r := range active {
		b.during.envs = append(b.during.envs, [3]string{r.Backend, r.TaskID, r.State})
		for _, g := range b.h.rows(job.Owner, "SELECT task_id, hosts, approved_by FROM sandbox_grants WHERE task_id = $1 AND state = 'active'", r.TaskID) {
			b.during.grants = append(b.during.grants, [3]string{fmt.Sprint(g["task_id"]), fmt.Sprint(g["hosts"]), fmt.Sprint(g["approved_by"])})
		}
	}
	switch b.mode {
	case "timeout":
		return envbuild.Outcome{Reason: fmt.Sprintf("构建超时（%d 秒）", int(job.Timeout.Seconds())), Failure: "timed_out"}, nil
	case "error":
		return envbuild.Outcome{}, providererr.New(providererr.Transient, "kube_unavailable", "连不上 Kubernetes API")
	case "reason":
		return envbuild.Outcome{Reason: b.reason, Failure: "failed"}, nil
	}
	layer := []byte("tool-versions-layer")
	if b.mode == "big" {
		layer = []byte(strings.Repeat("x", 4096))
	}
	_, err = out.Write(envbuildtest.OCITar([][]byte{layer}, envbuildtest.TarOptions{}))
	return envbuild.Outcome{OK: true}, err
}

type envSetup struct {
	h       *harness
	builder *fakeBuilder
	reg     *envbuildtest.Registry
	mu      sync.Mutex
	started [][2]string
}

func (s *envSetup) startedKeys() [][2]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([][2]string{}, s.started...)
}

type envOptions struct {
	limits     envbuild.Limits
	inProcess  bool
	noBuilder  bool
	heartbeat  time.Duration
	harnessOpt []option
}

func newEnvSetup(t *testing.T, o envOptions) *envSetup {
	t.Helper()
	s := &envSetup{reg: envbuildtest.NewRegistry(t)}
	if o.limits == (envbuild.Limits{}) {
		o.limits = envbuild.DefaultLimits()
	}
	opts := append([]option{withBackends(customImages), withGateways("http://pkg-mirror.test:8093", "http://egress.test:8095"),
		withEnvironments(func(h *harness) *envbuild.Manager {
			s.builder = &fakeBuilder{h: h, mode: "ok"}
			pusher, err := envbuild.NewRegistryClient(s.reg.URL, "pusher", "s3cret", "127.0.0.1:30500")
			if err != nil {
				t.Fatal(err)
			}
			cfg := envbuild.Config{BaseImage: envBase, BaseDigest: envBaseDigest, Registry: envbuild.RegistryPath{Host: "127.0.0.1:30500", Prefix: "envs"},
				Pusher: pusher, Limits: o.limits, BuildHosts: buildHosts, TmpDir: t.TempDir(), Heartbeat: o.heartbeat}
			if !o.noBuilder {
				cfg.Builder = s.builder
			}
			if !o.inProcess {
				cfg.Start = func(owner, key string) {
					s.mu.Lock()
					defer s.mu.Unlock()
					s.started = append(s.started, [2]string{owner, key})
				}
			}
			return envbuild.New(cfg)
		})}, o.harnessOpt...)
	s.h = newHarness(t, opts...)
	s.h.mkWorkspace("t1", "w1", nil)
	return s
}

var t1w1 = &commonv1.TenantContext{TenantId: "t1", UserId: "u1", WorkspaceId: "w1", SessionId: s1}

func (s *envSetup) write(files map[string]string) { s.h.mkWorkspace("t1", "w1", files) }

func (s *envSetup) remove(name string) {
	_ = os.Remove(filepath.Join(s.h.workspaces, "t1", "w1", name))
}

func (s *envSetup) resolve(t *testing.T) envbuild.Resolution {
	t.Helper()
	root := filepath.Join(s.h.workspaces, "t1", "w1")
	res, err := s.h.env.ResolveDefinition(context.Background(), t1w1, func(path string) ([]byte, error) {
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(path)))
		if os.IsNotExist(err) {
			return nil, nil
		}
		return data, err
	})
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func (s *envSetup) run(t *testing.T, key string) envbuild.ImageRecord {
	t.Helper()
	rec, err := s.h.env.RunBuild(context.Background(), "t1", key)
	if err != nil {
		t.Fatal(err)
	}
	return rec
}

// --- 构建、缓存与退回 ---

func TestProjectEnvironmentFirstUseBuildsAndSecondUseHitsTheCache(t *testing.T) {
	firstUseBuildsSecondHits(t, envOptions{})
}

func TestProjectEnvironmentFirstUseBuildsAndSecondUseHitsTheCachePostgres(t *testing.T) {
	firstUseBuildsSecondHits(t, envOptions{harnessOpt: []option{withPostgres()}})
}

func firstUseBuildsSecondHits(t *testing.T, o envOptions) {
	s := newEnvSetup(t, o)
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.resolve(t)
	if first.State != "building" || first.Image != "" || !strings.Contains(first.Note, "universal") || !reflect.DeepEqual(s.startedKeys(), [][2]string{{"t1", first.Key}}) {
		t.Fatalf("第一次：%+v %v", first, s.startedKeys())
	}
	rec := s.run(t, first.Key)
	if rec.State != "ready" || !strings.HasPrefix(rec.Image, "127.0.0.1:30500/envs/t1/env@sha256:") || rec.BuildMS < 0 {
		t.Fatalf("构建好：%+v", rec)
	}
	job := s.builder.jobs[0]
	if !strings.HasPrefix(job.Dockerfile, "# syntax=docker/dockerfile:1.7") || !strings.Contains(job.Dockerfile, "ghcr.io/agentruntime/sandbox-universal@"+envBaseDigest) {
		t.Fatalf("基础镜像按 digest 固定：%s", job.Dockerfile)
	}
	if !strings.HasPrefix(job.Env["HTTPS_PROXY"], "http://ar:ar1.") || job.Env["PIP_INDEX_URL"] == "" {
		t.Fatalf("构建经出网网关与包镜像：%v", job.Env)
	}
	if _, ok := s.reg.Manifest("envs/t1/env", first.Key[:32]); !ok || !reflect.DeepEqual(s.reg.Repos(), map[string]bool{"envs/t1/env": true}) {
		t.Fatalf("产物只推到本租户的路径：%v", s.reg.Repos())
	}
	// 构建期间：环境表里一行 env-builder、一条平台批准的授权（只放行登记的主机）；结束后都收回。
	if len(s.builder.during.envs) != 1 || s.builder.during.envs[0][0] != broker.BuildBackend || s.builder.during.envs[0][2] != "ready" ||
		!strings.HasPrefix(s.builder.during.envs[0][1], "envbuild-") {
		t.Fatalf("构建期间的环境表：%v", s.builder.during.envs)
	}
	task := s.builder.during.envs[0][1]
	if want := [][3]string{{task, `["auth.docker.io","production.cloudflare.docker.com","registry-1.docker.io"]`, "platform:env-builder"}}; !reflect.DeepEqual(s.builder.during.grants, want) {
		t.Fatalf("构建期间的授权：%v", s.builder.during.grants)
	}
	if len(s.h.rows("t1", "SELECT env_ref FROM sandbox_envs WHERE state <> 'destroyed'")) != 0 ||
		len(s.h.rows("t1", "SELECT grant_id FROM sandbox_grants WHERE task_id = $1 AND state = 'active'", task)) != 0 {
		t.Fatal("构建结束后环境与授权都收回")
	}
	second := s.resolve(t)
	if second.State != "ready" || second.Image != rec.Image || len(s.startedKeys()) != 1 {
		t.Fatalf("第二次直接用缓存，不再构建：%+v", second)
	}
	// 改了定义：新的键，重新构建。
	s.write(map[string]string{".tool-versions": strings.Replace(toolVersions, "20.18.0", "22.11.0", 1)})
	if third := s.resolve(t); third.State != "building" || third.Key == first.Key {
		t.Fatalf("改了定义：%+v", third)
	}
}

func TestFailedBuildsFallBackToUniversalWithTheReason(t *testing.T) {
	for mode, reason := range map[string]string{"timeout": "超时", "big": "大小上限", "error": "构建器出错"} {
		t.Run(mode, func(t *testing.T) {
			limits := envbuild.DefaultLimits()
			limits.MaxImageBytes, limits.BuildTimeout = 2048, 5*time.Minute
			s := newEnvSetup(t, envOptions{limits: limits})
			s.write(map[string]string{".tool-versions": toolVersions})
			building := s.resolve(t)
			s.builder.mode = mode
			rec := s.run(t, building.Key)
			if rec.State != "failed" || !strings.Contains(rec.Reason, reason) {
				t.Fatalf("%+v", rec)
			}
			again := s.resolve(t)
			if again.State != "failed" || !strings.Contains(again.Note, reason) || !strings.Contains(again.Note, "universal") || len(s.startedKeys()) != 1 {
				t.Fatalf("同一个定义不再反复构建：%+v", again)
			}
			if mode == "timeout" && s.builder.jobs[0].Timeout != 5*time.Minute {
				t.Fatal("构建的超时按登记的")
			}
		})
	}
}

func TestUnsupportedAndMaliciousDefinitionsNeverStartABuild(t *testing.T) {
	s := newEnvSetup(t, envOptions{})
	s.write(map[string]string{"Dockerfile": "FROM python:3.13\nRUN --network=host curl http://10.0.0.1/\n"})
	if res := s.resolve(t); res.State != "unsupported" || !strings.Contains(res.Note, "network=host") || !strings.Contains(res.Note, "universal") {
		t.Fatalf("%+v", res)
	}
	s.remove("Dockerfile")
	s.write(map[string]string{".tool-versions": "ruby 3.3.0\n"})
	if res := s.resolve(t); res.State != "unsupported" || !strings.Contains(res.Note, "ruby") {
		t.Fatalf("%+v", res)
	}
	if len(s.startedKeys()) != 0 {
		t.Fatal("不起构建")
	}
	plain := newEnvSetup(t, envOptions{noBuilder: true})
	plain.write(map[string]string{".tool-versions": toolVersions})
	if res := plain.resolve(t); res.State != "unsupported" || !strings.Contains(res.Note, "没有配置环境构建器") {
		t.Fatalf("没有构建器：%+v", res)
	}
}

func TestImageQuotaAndIdleCleanup(t *testing.T) {
	limits := envbuild.DefaultLimits()
	limits.MaxImages, limits.IdleDays = 1, 14
	s := newEnvSetup(t, envOptions{limits: limits})
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.resolve(t)
	s.run(t, first.Key)
	s.write(map[string]string{".tool-versions": "golang 1.23.2\n"})
	if other := s.resolve(t); other.State != "quota" || !strings.Contains(other.Note, "最多 1 个") || len(s.startedKeys()) != 1 {
		t.Fatalf("个数配额：%+v", other)
	}
	s.h.clock.Advance(15 * 24 * time.Hour)
	if n, err := s.h.env.GC(context.Background(), "t1"); err != nil || n != 1 {
		t.Fatalf("15 天没用的镜像清掉：%d %v", n, err)
	}
	if del := s.reg.DeletedList(); len(del) != 1 || del[0].Repo != "envs/t1/env" {
		t.Fatalf("删仓库里的清单：%v", del)
	}
	if rec, _ := s.h.env.Store.Get(context.Background(), "t1", first.Key); rec == nil || rec.State != "deleted" {
		t.Fatalf("记为 deleted：%+v", rec)
	}
	if retry := s.resolve(t); retry.State != "building" {
		t.Fatalf("清掉之后个数又够了：%+v", retry)
	}
}

func TestStoredFailureReasonsDropSignatureFragments(t *testing.T) {
	const signature = "Qm9ndXNTaWduYXR1cmVGb3JUZXN0c0-xYz7~AbCdEfGhIjKlMnOpQrStUv-WxYz0123456789~QwErTyUiOpAsDfGhJkLzXcVbNm-Plo9Okm8Ijn7Uhb"
	s := newEnvSetup(t, envOptions{})
	s.write(map[string]string{".tool-versions": toolVersions})
	building := s.resolve(t)
	s.builder.mode = "reason"
	s.builder.reason = "构建失败：" + signature + "~Tr4nc4ted~Fr4gm3ntOfAS1gnedUrl-ZxCvBnMaSdFgHjKlQwErTyUiOp1234&Key-Pair: i/o timeout"
	rec := s.run(t, building.Key)
	if rec.State != "failed" || !strings.Contains(rec.Reason, "i/o timeout") || strings.Contains(rec.Reason, signature) || strings.Contains(rec.Reason, "Fr4gm3ntOfAS1gnedUrl") {
		t.Fatalf("%q", rec.Reason)
	}
	if again := s.resolve(t); again.State != "failed" || strings.Contains(again.Note, signature) || !strings.Contains(again.Note, "universal") {
		t.Fatalf("%+v", again)
	}
}

// --- 失败之后（TMP 计划第 11.15 节，调优第一轮） ---

func failedOnce(t *testing.T, mod func(*envbuild.Limits)) (*envSetup, string) {
	t.Helper()
	limits := envbuild.DefaultLimits()
	if mod != nil {
		mod(&limits)
	}
	s := newEnvSetup(t, envOptions{limits: limits})
	s.write(map[string]string{".tool-versions": toolVersions})
	building := s.resolve(t)
	s.builder.mode = "error"
	if rec := s.run(t, building.Key); rec.State != "failed" {
		t.Fatalf("%+v", rec)
	}
	return s, building.Key
}

func TestAFailureIsNotRetriedRightAway(t *testing.T) {
	s, _ := failedOnce(t, nil)
	s.h.clock.Advance(10 * time.Minute)
	if again := s.resolve(t); again.State != "failed" || len(s.startedKeys()) != 1 {
		t.Fatalf("不能每次都起一次要跑很久的构建：%+v", again)
	}
}

func TestAFailureCanBeRetriedAfterAWhile(t *testing.T) {
	s, key := failedOnce(t, nil)
	s.h.clock.Advance(time.Hour + time.Second)
	if again := s.resolve(t); again.State != "building" || again.Key != key || len(s.startedKeys()) != 2 {
		t.Fatalf("%+v", again)
	}
	s.builder.mode = "ok"
	if rec := s.run(t, key); rec.State != "ready" {
		t.Fatalf("这次成功了，就用得上：%+v", rec)
	}
}

func TestTheRetryWaitIsConfigurable(t *testing.T) {
	s, _ := failedOnce(t, func(l *envbuild.Limits) { l.FailedRetryAfter = 5 * time.Minute })
	s.h.clock.Advance(6 * time.Minute)
	if again := s.resolve(t); again.State != "building" || len(s.startedKeys()) != 2 {
		t.Fatalf("%+v", again)
	}
}

func TestRetryingRespectsTheImageQuota(t *testing.T) {
	s, _ := failedOnce(t, func(l *envbuild.Limits) { l.MaxImages = 0 })
	s.h.clock.Advance(time.Hour + time.Second)
	s.resolve(t)
	if len(s.startedKeys()) != 2 {
		t.Fatal("max_images 为 0 表示不限，照样能重试")
	}
}

func TestTheEndOfALongFailureReasonIsKept(t *testing.T) {
	s, key := failedOnce(t, nil)
	if err := s.h.env.Store.MarkFailed(context.Background(), "t1", key, "构建失败："+strings.Repeat("#4 extracting sha256:abc\n", 100)+"ERROR: no space left on device", 0); err != nil {
		t.Fatal(err)
	}
	rec, _ := s.h.env.Store.Get(context.Background(), "t1", key)
	if !strings.HasSuffix(rec.Reason, "ERROR: no space left on device") || len([]rune(rec.Reason)) > 500 {
		t.Fatalf("留尾部：%q", rec.Reason)
	}
}

// --- 构建在服务进程里；心跳停了由别人接手 ---

func TestBuildsRunInTheServiceProcess(t *testing.T) {
	s := newEnvSetup(t, envOptions{inProcess: true, heartbeat: 20 * time.Millisecond})
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.resolve(t)
	deadline := time.Now().Add(10 * time.Second)
	for {
		rec, err := s.h.env.Store.Get(context.Background(), "t1", first.Key)
		if err != nil {
			t.Fatal(err)
		}
		if rec.State == "ready" {
			break
		}
		if rec.State != "building" || time.Now().After(deadline) {
			t.Fatalf("在进程里构建完：%+v", rec)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if res := s.resolve(t); res.State != "ready" {
		t.Fatalf("%+v", res)
	}
}

func TestAStuckBuildIsTakenOverWhenItsHeartbeatStops(t *testing.T) {
	stuckBuildTakenOver(t, envOptions{})
}

func TestAStuckBuildIsTakenOverWhenItsHeartbeatStopsPostgres(t *testing.T) {
	stuckBuildTakenOver(t, envOptions{harnessOpt: []option{withPostgres()}})
}

func stuckBuildTakenOver(t *testing.T, o envOptions) {
	s := newEnvSetup(t, o)
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.resolve(t)
	if again := s.resolve(t); again.State != "building" || len(s.startedKeys()) != 1 {
		t.Fatalf("心跳还新：不接手（%+v）", again)
	}
	// 起构建的服务没了：心跳停在 10 分钟前。
	s.h.execSQL("t1", "UPDATE sandbox_images SET last_used_at = $1 WHERE image_key = $2", ledger.ISO(s.h.clock.Now().Add(-10*time.Minute)), first.Key)
	if again := s.resolve(t); again.State != "building" || len(s.startedKeys()) != 2 {
		t.Fatalf("接手重来：%+v %v", again, s.startedKeys())
	}
	if again := s.resolve(t); again.State != "building" || len(s.startedKeys()) != 2 {
		t.Fatal("接手时记了心跳：只接手一次")
	}
	if rec := s.run(t, first.Key); rec.State != "ready" {
		t.Fatalf("%+v", rec)
	}
}

// --- shell_run 一侧（Acquire 带 project_environment） ---

// acceptsImages 包一层开发实现：接受 spec.image（集群里的 K8s 后端用环境镜像），记下每次建环境用的镜像，建出来时去掉它。
type acceptsImages struct {
	sandbox.Provider
	mu     *sync.Mutex
	images *[]string
}

func (p acceptsImages) Bind(tc *commonv1.TenantContext) (sandbox.Sandbox, error) {
	inner, err := p.Provider.Bind(tc)
	return acceptsImagesBox{Sandbox: inner, p: p}, err
}

type acceptsImagesBox struct {
	sandbox.Sandbox
	p acceptsImages
}

func (b acceptsImagesBox) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	b.p.mu.Lock()
	*b.p.images = append(*b.p.images, spec.Image)
	b.p.mu.Unlock()
	image := spec.Image
	spec.Image = ""
	env, err := b.Sandbox.Create(ctx, spec)
	if image == "" {
		image = envBaseDigest
	}
	env.Image = image
	return env, err
}

func projectEnv() acquireOpt {
	return func(r *sandboxv1.AcquireRequest) { r.ProjectEnvironment = true }
}

func environmentOf(t *testing.T, lease *sandboxv1.Lease) map[string]any {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal([]byte(lease.GetEnvironmentJson()), &doc); err != nil {
		t.Fatalf("environment_json：%q", lease.GetEnvironmentJson())
	}
	return doc
}

func TestShellRunSaysWhichEnvironmentItUsed(t *testing.T) {
	s := newEnvSetup(t, envOptions{})
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.h.mustAcquire(t1w1, onWorkspace("w1"), prepare(), projectEnv())
	env := environmentOf(t, first)
	if env["state"] != "building" || !strings.Contains(fmt.Sprint(env["note"]), "universal") || env["definition"] != ".tool-versions" || env["kind"] != "tool-versions" {
		t.Fatalf("第一次：%v", env)
	}
	if _, err := s.h.release(t1w1, first, false); err != nil {
		t.Fatal(err)
	}
	keys := s.startedKeys()
	s.run(t, keys[0][1])
	// 开发实现不用镜像：要用项目环境的镜像时退回 universal，并说明（集群里的 K8s 后端会用上它）。
	second := s.h.mustAcquire(t1w1, onWorkspace("w1"), prepare(), projectEnv())
	if env := environmentOf(t, second); env["state"] != "fallback" || !strings.Contains(fmt.Sprint(env["note"]), "universal") {
		t.Fatalf("退回 universal：%v", env)
	}
	if out := s.h.mustExec(t1w1, second, "echo hi"); out != "hi\n" {
		t.Fatalf("命令照常执行：%q", out)
	}
	// 不要项目环境的调用（Workspace 操作）不带这一节。
	if _, err := s.h.release(t1w1, second, false); err != nil {
		t.Fatal(err)
	}
	if plain := s.h.mustAcquire(t1w1, onWorkspace("w1")); plain.GetEnvironmentJson() != "" {
		t.Fatalf("%q", plain.GetEnvironmentJson())
	}
}

func TestTheBuiltImageIsUsedWhenTheBackendTakesImages(t *testing.T) {
	var mu sync.Mutex
	var images []string
	s := newEnvSetup(t, envOptions{harnessOpt: []option{withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider {
		return acceptsImages{Provider: p, mu: &mu, images: &images}
	})}})
	s.write(map[string]string{".tool-versions": toolVersions})
	first := s.h.mustAcquire(t1w1, onWorkspace("w1"), projectEnv(), reuse())
	if _, err := s.h.release(t1w1, first, true); err != nil {
		t.Fatal(err)
	}
	rec := s.run(t, s.startedKeys()[0][1])
	// 镜像好了：闲着的旧环境换掉，新环境用构建好的镜像。
	second := s.h.mustAcquire(t1w1, onWorkspace("w1"), projectEnv(), reuse())
	if env := environmentOf(t, second); env["state"] != "ready" {
		t.Fatalf("%v", env)
	}
	if second.GetReused() || second.GetRecord().GetImage() != rec.Image || images[len(images)-1] != rec.Image {
		t.Fatalf("用构建好的镜像：%v %v", second.GetRecord().GetImage(), images)
	}
}

// --- setup 的缓存（ENV-02） ---

const setupScript = "echo setup >> order.txt\n"

func (s *envSetup) order(t *testing.T) []string {
	data, _ := os.ReadFile(filepath.Join(s.h.workspaces, "t1", "w1", "order.txt"))
	return strings.Fields(string(data))
}

func (s *envSetup) bindEnvironment(t *testing.T, script string) {
	t.Helper()
	if err := s.h.svc.Broker().Archiver().Ensure(context.Background(), "t1", "w1", registry.WorkspaceLimits{}); err != nil {
		t.Fatal(err)
	}
	setEnvironment(s.h, "t1", "w1", fmt.Sprintf(`{"environment_id":"env-py","network":{"hosts":[],"mode":""},"setup_script":%q,"start_command":"echo start >> order.txt\n"}`, script))
}

func (s *envSetup) shell(t *testing.T) *sandboxv1.Lease {
	t.Helper()
	lease := s.h.mustAcquire(t1w1, onWorkspace("w1"), prepare())
	s.h.mustExec(t1w1, lease, "echo cmd >> order.txt")
	if _, err := s.h.release(t1w1, lease, false); err != nil {
		t.Fatal(err)
	}
	return lease
}

func TestSuccessfulSetupIsCachedByScriptAndBaseImage(t *testing.T) {
	var mu sync.Mutex
	var images []string
	s := newEnvSetup(t, envOptions{harnessOpt: []option{withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider {
		return acceptsImages{Provider: p, mu: &mu, images: &images}
	})}})
	s.bindEnvironment(t, setupScript)
	s.shell(t)
	key := broker.SetupKey(setupScript, envBaseDigest)
	if !reflect.DeepEqual(s.startedKeys(), [][2]string{{"t1", key}}) {
		t.Fatalf("setup 成功后按缓存键起一次构建：%v", s.startedKeys())
	}
	rec, _ := s.h.env.Store.Get(context.Background(), "t1", key)
	if rec == nil || rec.Kind != "setup" || rec.Path != "environment:env-py" || !strings.Contains(rec.Dockerfile, "base64 -d") || rec.Context != "" {
		t.Fatalf("缓存的构建记录：%+v", rec)
	}
	built := s.run(t, key)
	if built.State != "ready" {
		t.Fatalf("%+v", built)
	}
	// 下一个新环境用缓存的镜像，不再跑 setup。
	second := s.shell(t)
	if got := s.order(t); !reflect.DeepEqual(got, []string{"setup", "start", "cmd", "start", "cmd"}) {
		t.Fatalf("命中缓存：setup 不重跑，start 照跑：%v", got)
	}
	if second.GetPrepared().GetSetup() != "cached" || images[len(images)-1] != built.Image {
		t.Fatalf("用缓存的镜像：%v %v", second.GetPrepared(), images)
	}
	// 脚本变了：缓存不中，重跑。
	s.bindEnvironment(t, "echo setup2 >> order.txt\n")
	s.shell(t)
	if got := s.order(t); !reflect.DeepEqual(got[len(got)-3:], []string{"setup2", "start", "cmd"}) {
		t.Fatalf("%v", got)
	}
}

func TestFailedSetupNeverStartsACacheBuild(t *testing.T) {
	s := newEnvSetup(t, envOptions{})
	s.bindEnvironment(t, "exit 1\n")
	_, err := s.h.acquire(t1w1, onWorkspace("w1"), prepare())
	if _, c := code(t, err); c != "environment_setup_failed" {
		t.Fatalf("%v", err)
	}
	if len(s.startedKeys()) != 0 {
		t.Fatal("只缓存成功的结果")
	}
}
