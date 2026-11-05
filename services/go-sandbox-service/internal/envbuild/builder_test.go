package envbuild

// 构建的 Job（从 py-worker 的 test_sandbox_environments.py 搬来）：锁死的清单、结果的分类、日志里的凭据。

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild/envbuildtest"
)

func jobFor(mod func(*BuildJob)) BuildJob {
	j := BuildJob{Owner: "t1", Key: strings.Repeat("ab", 32), Dockerfile: "FROM python:3.13\n", Context: ".", WorkspaceID: "w1",
		Timeout: 20 * time.Minute, MaxBytes: 4 << 30,
		Env: map[string]string{"HTTPS_PROXY": "http://ar:ar1.body.sig@egress:8095", "PIP_INDEX_URL": "http://m/t/ar1.a.b/pypi/simple/"}}
	if mod != nil {
		mod(&j)
	}
	return j
}

func podStatus(build map[string]any, ready bool, extra map[string]any) map[string]any {
	status := map[string]any{"podIP": "10.42.0.17", "containerStatuses": []any{map[string]any{"name": "serve", "ready": ready}}}
	if build != nil {
		status["initContainerStatuses"] = []any{map[string]any{"name": "build", "state": build}}
	}
	for k, v := range extra {
		status[k] = v
	}
	return status
}

var (
	done    = map[string]any{"terminated": map[string]any{"exitCode": 0, "reason": "Completed"}}
	running = map[string]any{"running": map[string]any{}}
)

// fakeKube：Job 的状态固定，Pod 的状态按调用次数依次给出（最后一个一直沿用）。
type fakeKube struct {
	mu        sync.Mutex
	jobStatus map[string]any
	pods      []map[string]any
	log       string
	leftovers map[string][]string
	calls     [][2]string
	bodies    []any
}

func newFakeKube(jobStatus map[string]any, pods ...map[string]any) *fakeKube {
	if len(pods) == 0 {
		pods = []map[string]any{podStatus(nil, false, nil)}
	}
	return &fakeKube{jobStatus: jobStatus, pods: pods}
}

func (k *fakeKube) Request(_ context.Context, method, path string, body any, _ string) (int, []byte, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls = append(k.calls, [2]string{method, path})
	if strings.HasSuffix(path, "/log?container=build&tailLines=20") {
		return 200, []byte(k.log), nil
	}
	return 200, []byte("{}"), nil
}

func (k *fakeKube) JSON(_ context.Context, method, path string, body any, out any) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls = append(k.calls, [2]string{method, path})
	k.bodies = append(k.bodies, body)
	var resp any = map[string]any{}
	switch {
	case method == http.MethodGet && strings.Contains(path, "?labelSelector=") && !strings.Contains(path, "job-name"):
		var items []any
		for kind, names := range k.leftovers {
			if strings.Contains(path, "/"+kind+"?") {
				for _, n := range names {
					items = append(items, map[string]any{"metadata": map[string]any{"name": n}})
				}
			}
		}
		resp = map[string]any{"items": items}
	case method == http.MethodGet && strings.Contains(path, "/jobs/"):
		resp = map[string]any{"status": k.jobStatus}
	case method == http.MethodGet && strings.Contains(path, "/pods?labelSelector=job-name%3D"):
		status := k.pods[0]
		if len(k.pods) > 1 {
			k.pods = k.pods[1:]
		}
		resp = map[string]any{"items": []any{map[string]any{"metadata": map[string]any{"name": "envb-pod"}, "status": status}}}
	}
	if out != nil {
		data, _ := json.Marshal(resp)
		return json.Unmarshal(data, out)
	}
	return nil
}

func (k *fakeKube) deletes() []string {
	var out []string
	for _, c := range k.calls {
		if c[0] == http.MethodDelete {
			out = append(out, c[1])
		}
	}
	return out
}

func (k *fakeKube) secret() map[string]any {
	for _, b := range k.bodies {
		if m, ok := b.(map[string]any); ok && m["kind"] == "Secret" {
			return m
		}
	}
	return nil
}

// fakeFetch 记下地址与令牌，写一个 OCI 打包；给了 size 就写那么多字节（看大小上限）。
type fakeFetch struct {
	size  int
	calls [][2]string
}

func (f *fakeFetch) fetch(_ context.Context, url, token string, out io.Writer, limit int64) (int64, error) {
	f.calls = append(f.calls, [2]string{url, token})
	data := envbuildtest.OCITar(nil, envbuildtest.TarOptions{})
	if f.size > 0 {
		data = bytes.Repeat([]byte("x"), f.size)
	}
	if int64(len(data)) > limit {
		return 0, providererr.New(providererr.Invalid, "image_too_large", "构建产物超过大小上限")
	}
	n, err := out.Write(data)
	return int64(n), err
}

var testCfg = BuilderConfig{AgentImage: "ghcr.io/agentruntime/sandbox-agent:0.1.0", WorkspaceClaim: "workspaces", PollInterval: time.Millisecond}

func builder(t *testing.T, k Kube, f Fetch) *K8sJobBuilder {
	t.Helper()
	b, err := NewK8sJobBuilder(k, testCfg, f)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func get(m any, path ...any) any {
	for _, p := range path {
		switch k := p.(type) {
		case string:
			m = m.(map[string]any)[k]
		case int:
			m = m.([]any)[k]
		}
	}
	return m
}

func named(list any, name string) map[string]any {
	for _, v := range list.([]any) {
		if m := v.(map[string]any); m["name"] == name {
			return m
		}
	}
	return nil
}

func TestBuildJobIsLockedDown(t *testing.T) {
	b := builder(t, newFakeKube(nil), nil)
	configMap, secret, job, err := b.Manifests(jobFor(nil), "envb-x", strings.Repeat("T", 43))
	if err != nil {
		t.Fatal(err)
	}
	spec := get(job, "spec", "template", "spec").(map[string]any)
	if get(job, "metadata", "namespace") != "sandbox-builds" || spec["runtimeClassName"] != "kata" {
		t.Fatal("在 sandbox-builds 命名空间、Kata 里")
	}
	if spec["automountServiceAccountToken"] != false || spec["hostNetwork"] != false || spec["hostPID"] != false || spec["hostIPC"] != false {
		t.Fatal("不挂 ServiceAccount 令牌、不用宿主的网络、PID、IPC")
	}
	for _, v := range spec["volumes"].([]any) {
		if _, ok := v.(map[string]any)["hostPath"]; ok {
			t.Fatal("不挂宿主路径")
		}
	}
	if get(job, "spec", "activeDeadlineSeconds") != int64(1200+900) || get(job, "spec", "backoffLimit") != 0 {
		t.Fatal("构建的超时之外留出取回产物的时间")
	}
	build := get(spec, "initContainers", 0).(map[string]any)
	serve := get(spec, "containers", 0).(map[string]any)
	if p, _ := build["securityContext"].(map[string]any); len(p) != 1 || p["privileged"] != true {
		t.Fatal("特权只给构建（在 Kata 的 VM 里）")
	}
	if get(serve, "securityContext", "runAsNonRoot") != true || get(serve, "securityContext", "allowPrivilegeEscalation") != false {
		t.Fatal("提供产物的那一步不特权")
	}
	if named(build["env"], "AGENT_TOKEN") != nil {
		t.Fatal("构建拿不到取回产物的令牌")
	}
	tokenEnv := named(serve["env"], "AGENT_TOKEN")
	if ref := get(tokenEnv, "valueFrom", "secretKeyRef").(map[string]any); ref["name"] != "envb-x" || ref["key"] != "AGENT_TOKEN" ||
		named(serve["env"], "AGENT_WORKDIR")["value"] != "/out" {
		t.Fatal("令牌从这次的 Secret 来")
	}
	if get(serve, "command", 0) != "/sandbox-agent" || named(serve["volumeMounts"], "out")["readOnly"] != true {
		t.Fatal("只读地提供产物")
	}
	plain, _ := json.Marshal([]any{configMap, job})
	if strings.Contains(string(plain), strings.Repeat("T", 43)) || strings.Contains(string(plain), "ar1.body.sig") {
		t.Fatal("令牌与凭据只在这次构建的 Secret 里")
	}
	data := secret["stringData"].(map[string]any)
	if data["AGENT_TOKEN"] != strings.Repeat("T", 43) || !strings.HasPrefix(data["HTTPS_PROXY"].(string), "http://ar:") {
		t.Fatal("Secret 里有令牌与联网变量")
	}
	context := named(spec["volumes"], "context")
	mount := named(build["volumeMounts"], "context")
	if get(context, "persistentVolumeClaim", "readOnly") != true || mount["readOnly"] != true || mount["subPath"] != "t1/w1" {
		t.Fatal("只读、只有本租户的这个工作区")
	}
	if get(named(spec["volumes"], "out"), "emptyDir", "sizeLimit") != "4096Mi" {
		t.Fatal("产物超过大小上限时被驱逐")
	}
	if !strings.Contains(get(build, "command", 2).(string), "--output type=oci,dest=/out/image.tar") {
		t.Fatal("输出 OCI 打包")
	}
	// 别的写法一律拒绝。
	if _, err := NewK8sJobBuilder(newFakeKube(nil), BuilderConfig{AgentImage: "a", Isolation: "container", RuntimeClass: "gvisor"}, nil); err == nil {
		t.Fatal("不是 microVM 的拒绝")
	}
	if _, err := BuilderFromSpec(map[string]any{"agent_image": "a", "runtime_class": ""}, newFakeKube(nil)); err == nil {
		t.Fatal("runtime_class 写成空的拒绝")
	}
	if _, _, _, err := b.Manifests(jobFor(func(j *BuildJob) { j.Context = "../other-tenant" }), "envb-y", "T"); providererr.CategoryOf(err) != providererr.Invalid {
		t.Fatal("上下文出了仓库")
	}
	if _, _, _, err := b.Manifests(jobFor(func(j *BuildJob) { j.Owner = "../t2" }), "envb-y", "T"); providererr.CategoryOf(err) != providererr.Invalid {
		t.Fatal("租户的格式不对")
	}
	_, _, noContext, _ := b.Manifests(jobFor(func(j *BuildJob) { j.Context = "" }), "envb-z", "T")
	if get(named(get(noContext, "spec", "template", "spec", "volumes"), "context"), "emptyDir") == nil {
		t.Fatal("不用仓库里的文件时上下文是空目录")
	}
}

func TestBuildJobOutcomesAndRedactedLogs(t *testing.T) {
	ctx := context.Background()
	// 构建那一步正常退出、agent 就绪：按 Pod IP 带着这次的令牌取回，取完删掉 Job、Secret、ConfigMap。
	fetch := &fakeFetch{}
	ok := newFakeKube(nil, podStatus(running, false, nil), podStatus(done, false, nil), podStatus(done, true, nil))
	var out bytes.Buffer
	if o, err := builder(t, ok, fetch.fetch).Build(ctx, jobFor(nil), &out); err != nil || !o.OK {
		t.Fatalf("%+v %v", o, err)
	}
	token := ok.secret()["stringData"].(map[string]any)["AGENT_TOKEN"].(string)
	if len(fetch.calls) != 1 || fetch.calls[0][0] != "http://10.42.0.17:7070/v1/files/image.tar" || fetch.calls[0][1] != token || len(token) < 32 {
		t.Fatalf("取回：%v", fetch.calls)
	}
	if !bytes.Equal(out.Bytes(), envbuildtest.OCITar(nil, envbuildtest.TarOptions{})) {
		t.Fatal("产物原样写出")
	}
	deletes := strings.Join(ok.deletes(), " ")
	if len(ok.deletes()) != 3 || !strings.Contains(deletes, "/jobs/") || !strings.Contains(deletes, "/secrets/") || !strings.Contains(deletes, "/configmaps/") {
		t.Fatalf("Job、Secret、ConfigMap 都删：%v", ok.deletes())
	}
	// 构建超时：构建那一步还在跑就停；Job 自己的 activeDeadlineSeconds 也算超时。
	slow, _ := builder(t, newFakeKube(nil, podStatus(running, false, nil)), fetch.fetch).Build(ctx, jobFor(func(j *BuildJob) { j.Timeout = 20 * time.Millisecond }), io.Discard)
	if slow.OK || slow.Failure != "timed_out" {
		t.Fatalf("%+v", slow)
	}
	expired := newFakeKube(map[string]any{"conditions": []any{map[string]any{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}}})
	deadline, _ := builder(t, expired, fetch.fetch).Build(ctx, jobFor(nil), io.Discard)
	if deadline.OK || deadline.Failure != "timed_out" || !strings.Contains(deadline.Reason, "1200 秒") {
		t.Fatalf("%+v", deadline)
	}
	// 产物超过大小上限：被驱逐，或取回时超了。
	evicted, _ := builder(t, newFakeKube(nil, podStatus(nil, false, map[string]any{"reason": "Evicted"})), fetch.fetch).Build(ctx, jobFor(nil), io.Discard)
	if evicted.OK || evicted.Failure != "too_large" {
		t.Fatalf("%+v", evicted)
	}
	big, _ := builder(t, newFakeKube(nil, podStatus(done, true, nil)), (&fakeFetch{size: 2048}).fetch).Build(ctx, jobFor(func(j *BuildJob) { j.MaxBytes = 1024 }), io.Discard)
	if big.OK || big.Failure != "too_large" {
		t.Fatalf("%+v", big)
	}
	// 构建失败：附上构建日志的最后几行，去掉凭据。
	leaky := "ERROR: failed to fetch http://ar:ar1.body.sig@egress:8095 while resolving http://m/t/ar1.a.b/pypi/simple/"
	failedKube := newFakeKube(nil, podStatus(map[string]any{"terminated": map[string]any{"exitCode": 1, "reason": "Error"}}, false, nil))
	failedKube.log = leaky
	failed, _ := builder(t, failedKube, fetch.fetch).Build(ctx, jobFor(nil), io.Discard)
	if failed.OK || failed.Failure != "failed" || !strings.Contains(failed.Reason, "构建失败") {
		t.Fatalf("%+v", failed)
	}
	if strings.Contains(failed.Reason, "ar1.body.sig") || strings.Contains(failed.Reason, "ar1.a.b") {
		t.Fatalf("日志里的凭据去掉：%s", failed.Reason)
	}
	oom := newFakeKube(nil, podStatus(map[string]any{"terminated": map[string]any{"exitCode": 137, "reason": "OOMKilled"}}, false, nil))
	if o, _ := builder(t, oom, fetch.fetch).Build(ctx, jobFor(nil), io.Discard); !strings.Contains(o.Reason, "OOMKilled") {
		t.Fatalf("%+v", o)
	}
	if len(fetch.calls) != 1 {
		t.Fatal("失败、超时时不去取产物")
	}
}

// EV-TB：拉层失败时 BuildKit 打进日志的签名地址不进失败原因。
func TestBuildFailureReasonsDropSignedURLs(t *testing.T) {
	log := "#5 [1/2] FROM docker.io/swebench/sweb.eval.x86_64.django:latest\n" +
		"ERROR: failed to copy: httpReadSeeker: failed open: failed to do request: Get \"" + signedURL + "\": dial tcp: i/o timeout\n" +
		"retrying https://user:hunter2-password@mirror.example/v2/x?token=abcdef0123456789abcdef0123456789ab"
	k := newFakeKube(nil, podStatus(map[string]any{"terminated": map[string]any{"exitCode": 1, "reason": "Error"}}, false, nil))
	k.log = log
	failed, _ := builder(t, k, (&fakeFetch{}).fetch).Build(context.Background(), jobFor(nil), io.Discard)
	if failed.OK || !strings.Contains(failed.Reason, "构建失败") {
		t.Fatalf("%+v", failed)
	}
	for _, leaked := range []string{signature, "K2B7EXAMPLEKEYPAIR", "Expires=1730383200", "hunter2-password", "abcdef0123456789abcdef0123456789ab"} {
		if strings.Contains(failed.Reason, leaked) {
			t.Errorf("失败原因里不能有 %s…：%s", leaked[:12], failed.Reason)
		}
	}
	for _, kept := range []string{"production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/sha256/5f/", layerHex, "i/o timeout", "mirror.example"} {
		if !strings.Contains(failed.Reason, kept) {
			t.Errorf("要留着 %q", kept)
		}
	}
}

// 服务在构建中途重启时留下的同一个键的对象，下一次构建开始时删掉。
func TestLeftoversOfTheSameKeyAreSweptFirst(t *testing.T) {
	k := newFakeKube(nil, podStatus(done, true, nil))
	k.leftovers = map[string][]string{"jobs": {"envb-old-1"}, "secrets": {"envb-old-1"}, "configmaps": {"envb-old-1"}}
	if o, err := builder(t, k, (&fakeFetch{}).fetch).Build(context.Background(), jobFor(nil), io.Discard); err != nil || !o.OK {
		t.Fatalf("%+v %v", o, err)
	}
	var old int
	for _, d := range k.deletes() {
		if strings.HasSuffix(d, "/envb-old-1") {
			old++
		}
	}
	if old != 3 {
		t.Fatalf("旧的 Job、Secret、ConfigMap 都删：%v", k.deletes())
	}
	if !strings.Contains(k.calls[0][1], "agentruntime.io%2Fenv-key%3D"+strings.Repeat("ab", 16)) || !strings.Contains(k.calls[0][1], "managed-by%3D"+ManagedBy) {
		t.Fatalf("按这个键的标签找：%v", k.calls[0])
	}
}
