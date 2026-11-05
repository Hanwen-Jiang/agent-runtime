package envbuild

// OCI 打包的读与推（从 py-core 的 test_oci_registry.py 搬来）。

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild/envbuildtest"
)

func tarFile(t *testing.T, data []byte) *os.File {
	t.Helper()
	f, err := os.Create(filepath.Join(t.TempDir(), "image.tar"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write(data); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.Close() })
	return f
}

func wantCode(t *testing.T, err error, code string) {
	t.Helper()
	var pe *providererr.Error
	if err == nil || !errors.As(err, &pe) || pe.Code != code {
		t.Fatalf("要 %s，实际 %v", code, err)
	}
}

func TestReadLayoutFindsTheAmd64ManifestAndItsBlobs(t *testing.T) {
	for _, nested := range []bool{false, true} {
		l, err := ReadLayout(tarFile(t, envbuildtest.OCITar(nil, envbuildtest.TarOptions{Nested: nested})), 1<<20)
		if err != nil {
			t.Fatal(err)
		}
		var m struct {
			Config struct{ Digest string }   `json:"config"`
			Layers []struct{ Digest string } `json:"layers"`
		}
		_ = json.Unmarshal(l.Manifest, &m)
		want := []string{m.Config.Digest}
		for _, layer := range m.Layers {
			want = append(want, layer.Digest)
		}
		var got []string
		var size int64
		for _, b := range l.Blobs {
			got = append(got, b.Digest)
			size += b.Size
		}
		if strings.Join(got, ",") != strings.Join(want, ",") || l.Size() != size || !strings.HasPrefix(l.ManifestDigest(), "sha256:") {
			t.Fatalf("nested=%v：%v", nested, got)
		}
	}
}

func TestReadLayoutRefusesOversizeTamperedAndNonTar(t *testing.T) {
	_, err := ReadLayout(tarFile(t, envbuildtest.OCITar([][]byte{[]byte(strings.Repeat("x", 4096))}, envbuildtest.TarOptions{})), 1024)
	wantCode(t, err, "image_too_large")
	reg := envbuildtest.NewRegistry(t)
	l, err := ReadLayout(tarFile(t, envbuildtest.OCITar(nil, envbuildtest.TarOptions{Tamper: true})), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	client, _ := NewRegistryClient(reg.URL, "pusher", "s3cret", "")
	_, err = client.Push(context.Background(), "envs/t1/env", "k", l)
	wantCode(t, err, "invalid_image") // 推的时候边读边核对
	if _, ok := reg.Manifest("envs/t1/env", "k"); ok {
		t.Fatal("内容不对时不推清单")
	}
	_, err = ReadLayout(tarFile(t, []byte("not a tar")), 1<<20)
	if providererr.CategoryOf(err) != providererr.Invalid {
		t.Fatalf("不是 tar：%v", err)
	}
}

func TestPushPutsBlobsAndManifestUnderTheGivenPathOnly(t *testing.T) {
	ctx := context.Background()
	reg := envbuildtest.NewRegistry(t)
	client, err := NewRegistryClient(reg.URL, "pusher", "s3cret", "127.0.0.1:30500")
	if err != nil {
		t.Fatal(err)
	}
	l, err := ReadLayout(tarFile(t, envbuildtest.OCITar(nil, envbuildtest.TarOptions{})), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := client.Push(ctx, "envs/t1/env", "abc123", l)
	if err != nil || digest != l.ManifestDigest() {
		t.Fatalf("%s %v", digest, err)
	}
	if repos := reg.Repos(); len(repos) != 1 || !repos["envs/t1/env"] || len(reg.Blobs) != 3 {
		t.Fatalf("只推到给的路径：%v", repos)
	}
	if m, _ := reg.Manifest("envs/t1/env", "abc123"); string(m) != string(l.Manifest) {
		t.Fatal("清单原样推上去")
	}
	if got := client.Reference("envs/t1/env", digest); got != "127.0.0.1:30500/envs/t1/env@"+digest {
		t.Fatal(got)
	}
	uploads := reg.UploadCount()
	if again, err := client.Push(ctx, "envs/t1/env", "abc123", l); err != nil || again != digest || reg.UploadCount() != uploads {
		t.Fatalf("再推一次：已有的 blob 不重传（%d → %d）", uploads, reg.UploadCount())
	}
	if ok, err := client.Delete(ctx, "envs/t1/env", digest); !ok || err != nil {
		t.Fatalf("删：%v %v", ok, err)
	}
	if ok, err := client.Delete(ctx, "envs/t1/env", digest); ok || err != nil {
		t.Fatalf("再删：%v %v", ok, err)
	}
}

func TestBadCredentialsAndBadPathsAreRefused(t *testing.T) {
	ctx := context.Background()
	reg := envbuildtest.NewRegistry(t)
	l, err := ReadLayout(tarFile(t, envbuildtest.OCITar(nil, envbuildtest.TarOptions{})), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	bad, _ := NewRegistryClient(reg.URL, "pusher", "wrong", "")
	_, err = bad.Push(ctx, "envs/t1/env", "k", l)
	wantCode(t, err, "registry_denied")
	if strings.Contains(err.Error(), "wrong") {
		t.Fatal("错误信息里没有凭据")
	}
	client, _ := NewRegistryClient(reg.URL, "pusher", "s3cret", "")
	for _, c := range [][2]string{{"../t2/env", "k"}, {"envs/T1/env", "k"}, {"envs/t1/env", "a/b"}} {
		_, err := client.Push(ctx, c[0], c[1], l)
		wantCode(t, err, "invalid_registry_path")
	}
	if _, err := NewRegistryClient("ftp://x", "", "", ""); providererr.CategoryOf(err) != providererr.Invalid {
		t.Fatal("仓库地址要是 http 或 https")
	}
}
