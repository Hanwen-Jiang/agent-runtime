package s3objects_test

// STOR-SEC: only object CRUD under a unique prefix; never changes bucket lifecycle.
import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

// Run only this test against production: AR_LIVE_S3=1 go test ./adapters/s3objects -run '^TestLiveSignedUploadSecurity$' -v.
func TestLiveSignedUploadSecurity(t *testing.T) {
	if os.Getenv("AR_LIVE_S3") != "1" {
		t.Skip("AR_LIVE_S3=1 required")
	}
	for _, key := range []string{"AR_S3_ENDPOINT", "AR_S3_BUCKET", "AR_S3_ACCESS_KEY_ID", "AR_S3_SECRET_ACCESS_KEY"} {
		if os.Getenv(key) == "" {
			t.Fatalf("%s required", key)
		}
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal("random unavailable")
	}
	p, err := s3objects.New(s3objects.Options{Endpoint: os.Getenv("AR_S3_ENDPOINT"), Bucket: os.Getenv("AR_S3_BUCKET"),
		AccessKeyID: os.Getenv("AR_S3_ACCESS_KEY_ID"), SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")),
		Region: os.Getenv("AR_S3_REGION"), Prefix: "stor-sec/" + hex.EncodeToString(raw) + "/"})
	if err != nil {
		t.Fatal("object store configuration invalid")
	}
	s, err := p.Bind(conformance.TenantA())
	if err != nil {
		t.Fatal("bind failed")
	}
	cctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	client := &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	content := []byte("stor-sec expected bytes")
	sign := func(t *testing.T, key string, ttl time.Duration) objectstore.SignedUpload {
		t.Helper()
		up, e := s.SignedUpload(cctx, key, ttl, sha256Hex(string(content)), int64(len(content)))
		if e != nil {
			t.Fatal("sign failed")
		}
		t.Cleanup(func() {
			cleanup, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			if _, e := s.Delete(cleanup, key); e != nil {
				t.Error("object cleanup failed")
			}
		})
		return up
	}
	request := func(t *testing.T, method string, up objectstore.SignedUpload, data []byte) int {
		t.Helper()
		req, e := http.NewRequestWithContext(cctx, method, up.URL, bytes.NewReader(data))
		if e != nil {
			t.Fatal("request invalid")
		}
		for k, v := range up.Headers {
			if !strings.EqualFold(k, "Content-Length") {
				req.Header.Set(k, v)
			}
		}
		resp, e := client.Do(req)
		if e != nil {
			t.Fatal("storage transport failed (URL redacted)")
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 65536))
		return resp.StatusCode
	}
	reject := func(t *testing.T, status int) {
		t.Helper()
		if status < 400 || status >= 500 {
			t.Fatalf("expected explicit rejection, HTTP %d", status)
		}
	}
	t.Run("real-agent-upload-and-block-restore", func(t *testing.T) {
		binary := os.Getenv("AR_SANDBOX_AGENT_BIN")
		if binary == "" {
			t.Skip("AR_SANDBOX_AGENT_BIN required for real agent transport")
		}
		root := filepath.Join(t.TempDir(), "envs")
		provider, e := localsandbox.New(localsandbox.Options{Root: root, AgentBinary: binary})
		if e != nil {
			t.Fatal("agent setup failed")
		}
		t.Cleanup(func() { localsandbox.KillAll(root) })
		box, e := provider.Bind(conformance.TenantA())
		if e != nil {
			t.Fatal("agent scope failed")
		}
		env, e := box.Create(cctx, sandbox.Spec{})
		if e != nil {
			t.Fatal("agent start failed")
		}
		if box.WriteFile(cctx, env.ID, "data.bin", content) != nil {
			t.Fatal("agent write failed")
		}
		transfer := box.(sandbox.Transfers)
		prepared, e := transfer.PrepareTransfer(cctx, env.ID, "data.bin")
		if e != nil {
			t.Fatal("agent prepare failed")
		}
		up := sign(t, "agent", time.Minute)
		if box.WriteFile(cctx, env.ID, "data.bin", []byte("changed later")) != nil {
			t.Fatal("agent mutation failed")
		}
		receipt, e := transfer.CommitTransfer(cctx, env.ID, prepared, "agent", sandbox.TransferTarget{URL: up.URL, Headers: up.Headers})
		if e != nil || receipt.SHA256 != prepared.SHA256 {
			t.Fatal("real R2 agent upload failed")
		}
		if _, e = transfer.CommitTransfer(cctx, env.ID, prepared, "agent", sandbox.TransferTarget{URL: up.URL, Headers: up.Headers}); e != nil {
			t.Fatal("repeated agent commit failed")
		}
		got, e := s.Get(cctx, "agent")
		if e != nil || !bytes.Equal(got.Data, content) {
			t.Fatal("real R2 immutable bytes mismatch")
		}
		target, e := box.Create(cctx, sandbox.Spec{})
		if e != nil {
			t.Fatal("restore agent start failed")
		}
		url, e := s.TemporaryURL(cctx, "agent", time.Minute, "GET")
		if e != nil {
			t.Fatal("download signing failed")
		}
		restore := box.(sandbox.BlockTransfers)
		restoreID := strings.Repeat("a", 32)
		blocks := []sandbox.RestoreBlock{{SHA256: prepared.SHA256, SizeBytes: prepared.SizeBytes, Target: sandbox.TransferTarget{URL: url}}}
		if restore.RestoreBatch(cctx, target.ID, restoreID, 1<<20, blocks) != nil {
			t.Fatal("real R2 block download failed")
		}
		if restore.RestoreFinish(cctx, target.ID, restoreID, []sandbox.FileEntry{{Path: "restored.bin", Type: "file", Size: prepared.SizeBytes, Mode: 0600, SHA256: prepared.SHA256}}) != nil {
			t.Fatal("restore materialization failed")
		}
		data, e := box.ReadFile(cctx, target.ID, "restored.bin")
		if e != nil || !bytes.Equal(data, content) {
			t.Fatal("restored R2 bytes mismatch")
		}
	})
	t.Run("valid-and-no-overwrite", func(t *testing.T) {
		up := sign(t, "correct", time.Minute)
		if status := request(t, "PUT", up, content); status < 200 || status >= 300 {
			t.Fatalf("valid upload rejected HTTP %d", status)
		}
		reject(t, request(t, "PUT", up, content))
		got, e := s.Get(cctx, "correct")
		if e != nil || !bytes.Equal(got.Data, content) {
			t.Fatal("written content mismatch")
		}
	})
	t.Run("checksum-content", func(t *testing.T) {
		up := sign(t, "checksum", time.Minute)
		bad := append([]byte(nil), content...)
		bad[0] ^= 1
		reject(t, request(t, "PUT", up, bad))
	})
	t.Run("content-length", func(t *testing.T) {
		up := sign(t, "length", time.Minute)
		reject(t, request(t, "PUT", up, append(content, '!')))
	})
	for _, header := range []string{"If-None-Match", "x-amz-checksum-sha256"} {
		t.Run("missing-"+header, func(t *testing.T) {
			up := sign(t, "missing-"+header, time.Minute)
			delete(up.Headers, header)
			reject(t, request(t, "PUT", up, content))
		})
		t.Run("changed-"+header, func(t *testing.T) {
			up := sign(t, "changed-"+header, time.Minute)
			up.Headers[header] = "invalid"
			reject(t, request(t, "PUT", up, content))
		})
	}
	t.Run("key", func(t *testing.T) {
		up := sign(t, "key", time.Minute)
		u, _ := url.Parse(up.URL)
		u.Path += "-other"
		up.URL = u.String()
		reject(t, request(t, "PUT", up, content))
	})
	t.Run("method", func(t *testing.T) { up := sign(t, "method", time.Minute); reject(t, request(t, "POST", up, content)) })
	t.Run("expiry", func(t *testing.T) {
		up := sign(t, "expiry", time.Second)
		time.Sleep(3 * time.Second)
		reject(t, request(t, "PUT", up, content))
	})
}
