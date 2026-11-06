package s3objects_test

// STOR-SEC: only object CRUD under a unique prefix; never changes bucket lifecycle.
import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"net/http"
	"net/url"
	"os"
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
