// Package objectstoretest 是 ObjectStore 的一致性测试，与 Python 侧 agentruntime_core.conformance.object_store 一一对应。
package objectstoretest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
)

// Harness 由实现所在包提供。
type Harness interface {
	Provider() objectstore.Provider
	Advance(ctx context.Context, d time.Duration)
	conformance.Faultable
	// Fetch 用 TemporaryURL 生成的 GET 地址取对象；地址过期或无效时返回 Denied。
	Fetch(ctx context.Context, url string) ([]byte, error)
	// Upload 用 TemporaryURL 生成的 PUT 地址上传；地址过期或无效时返回 Denied。
	Upload(ctx context.Context, url string, data []byte) error
	// UploadSigned 用 SignedUpload 给的地址 PUT data，带上 headers（Content-Length 由 data 定）。被拒时返回错误：
	// 地址过期或签名不对 Denied，内容与签的哈希不符 Invalid，对象已存在 Conflict。
	UploadSigned(ctx context.Context, url string, data []byte, headers map[string]string) error
	// RunLifecycle 立即执行一次生命周期清理；做不到时返回 conformance.ErrUnsupported。
	RunLifecycle(ctx context.Context) error
}

var malformedKeys = []string{"", "/abs", "a//b", "a/../b", "./a", "a/.", "a\\b", "a/\nb", strings.Repeat("x", 1025)}

// Run 跑完整的 ObjectStore 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	probe := conformance.Probe[objectstore.Store]{
		Operation: "Put",
		Write: func(ctx context.Context, s objectstore.Store, name string) error {
			_, err := s.Put(ctx, "probes/"+name, []byte("probe"), objectstore.PutOptions{})
			return err
		},
		Visible: func(ctx context.Context, s objectstore.Store, name string) (bool, error) {
			_, err := s.Head(ctx, "probes/"+name)
			if providererr.CategoryOf(err) == providererr.NotFound {
				return false, nil
			}
			return err == nil, err
		},
	}
	binder := func(t *testing.T) conformance.Binder[objectstore.Store] { return newHarness(t).Provider() }
	conformance.RunBindingRules(t, binder)
	conformance.RunIsolationRules(t, binder, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[objectstore.Store], conformance.Faultable) {
		h := newHarness(t)
		return h.Provider(), h
	}, probe)

	ctx := context.Background()
	setup := func(t *testing.T) (Harness, objectstore.Store) {
		h := newHarness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}
	put := func(t *testing.T, s objectstore.Store, key string, data string) objectstore.ObjectInfo {
		t.Helper()
		return conformance.Must(s.Put(ctx, key, []byte(data), objectstore.PutOptions{}))
	}

	conformance.Case(t, "PutThenGet", func(t *testing.T) {
		_, s := setup(t)
		data := "华东退款明细\x00\xff"
		info := conformance.Must(s.Put(ctx, "reports/q3.csv", []byte(data), objectstore.PutOptions{
			ContentType: "text/csv", Metadata: map[string]string{"source": "q_region"},
		}))
		sum := sha256.Sum256([]byte(data))
		if info.Key != "reports/q3.csv" || info.Size != int64(len(data)) || info.SHA256 != hex.EncodeToString(sum[:]) || info.ContentType != "text/csv" {
			t.Fatalf("info = %+v", info)
		}
		got := conformance.Must(s.Get(ctx, "reports/q3.csv"))
		if string(got.Data) != data || got.Info.Metadata["source"] != "q_region" {
			t.Fatalf("got = %+v", got)
		}
		put(t, s, "empty", "")
		if len(conformance.Must(s.Get(ctx, "empty")).Data) != 0 {
			t.Fatal("empty object changed")
		}
	})
	conformance.Case(t, "URICarriesTenantPrefix", func(t *testing.T) {
		h, s := setup(t)
		info := put(t, s, "reports/q3.csv", "x")
		if info.URI != "obj://"+conformance.TenantAID+"/reports/q3.csv" || s.URI("reports/q3.csv") != info.URI {
			t.Fatalf("uri = %q", info.URI)
		}
		if conformance.Must(s.KeyOf(info.URI)) != "reports/q3.csv" {
			t.Fatal("KeyOf round trip failed")
		}
		platform := conformance.Must(h.Provider().BindPlatform("conformance"))
		if platform.URI("evals/set.json") != "obj://_platform/evals/set.json" {
			t.Fatalf("platform uri = %q", platform.URI("evals/set.json"))
		}
	})
	conformance.Case(t, "KeyOfOtherScopeIsDenied", func(t *testing.T) {
		h, s := setup(t)
		uri := put(t, s, "secret.txt", "x").URI
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		_, err := b.KeyOf(uri)
		conformance.ExpectError(t, err, providererr.Denied, "")
		_, err = s.KeyOf("obj://_platform/evals/set.json")
		conformance.ExpectError(t, err, providererr.Denied, "")
	})
	conformance.Case(t, "KeyOfMalformedURIIsInvalid", func(t *testing.T) {
		_, s := setup(t)
		for _, uri := range []string{"s3://bucket/key", "obj://", "obj://t-alpha", "obj://t-alpha/", "obj://T1/x"} {
			_, err := s.KeyOf(uri)
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "MissingIsNotFound", func(t *testing.T) {
		_, s := setup(t)
		_, err := s.Get(ctx, "missing")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = s.Head(ctx, "missing")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "PutIsIdempotentAndIfAbsentConflicts", func(t *testing.T) {
		_, s := setup(t)
		first := put(t, s, "k", "same")
		second := put(t, s, "k", "same")
		if first.SHA256 != second.SHA256 || len(conformance.Must(s.List(ctx, objectstore.ListOptions{})).Items) != 1 {
			t.Fatal("repeated Put was not idempotent")
		}
		conformance.Must(s.Put(ctx, "only", []byte("first"), objectstore.PutOptions{IfAbsent: true}))
		_, err := s.Put(ctx, "only", []byte("second"), objectstore.PutOptions{IfAbsent: true})
		conformance.ExpectError(t, err, providererr.Conflict, "")
		if string(conformance.Must(s.Get(ctx, "only")).Data) != "first" {
			t.Fatal("IfAbsent overwrote the object")
		}
	})
	conformance.Case(t, "DeleteReportsExistence", func(t *testing.T) {
		_, s := setup(t)
		put(t, s, "k", "x")
		if !conformance.Must(s.Delete(ctx, "k")) || conformance.Must(s.Delete(ctx, "k")) {
			t.Fatal("Delete must report true then false")
		}
		_, err := s.Get(ctx, "k")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "MalformedKeyIsInvalid", func(t *testing.T) {
		_, s := setup(t)
		for _, key := range malformedKeys {
			_, err := s.Put(ctx, key, []byte("x"), objectstore.PutOptions{})
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "ListByPrefixWithPagination", func(t *testing.T) {
		_, s := setup(t)
		for _, key := range []string{"a/3", "a/1", "b/1", "a/2"} {
			put(t, s, key, "x")
		}
		page := conformance.Must(s.List(ctx, objectstore.ListOptions{Prefix: "a/", Limit: 2}))
		if len(page.Items) != 2 || page.Items[0].Key != "a/1" || page.Items[1].Key != "a/2" || page.NextCursor == "" {
			t.Fatalf("page 1 = %+v", page)
		}
		page = conformance.Must(s.List(ctx, objectstore.ListOptions{Prefix: "a/", Limit: 2, Cursor: page.NextCursor}))
		if len(page.Items) != 1 || page.Items[0].Key != "a/3" || page.NextCursor != "" {
			t.Fatalf("page 2 = %+v", page)
		}
	})
	conformance.Case(t, "ListIsIsolatedPerTenant", func(t *testing.T) {
		h, s := setup(t)
		put(t, s, "shared-name", "x")
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		if items := conformance.Must(b.List(ctx, objectstore.ListOptions{})).Items; len(items) != 0 {
			t.Fatalf("tenant B listed %d objects of tenant A", len(items))
		}
	})
	conformance.Case(t, "TemporaryURLs", func(t *testing.T) {
		h, s := setup(t)
		put(t, s, "k", "payload")
		url := conformance.Must(s.TemporaryURL(ctx, "k", time.Minute, "GET"))
		if string(conformance.Must(h.Fetch(ctx, url))) != "payload" {
			t.Fatal("GET url did not return the object")
		}
		upload := conformance.Must(s.TemporaryURL(ctx, "uploads/x", time.Minute, "PUT"))
		conformance.NoError(t, h.Upload(ctx, upload, []byte("uploaded")))
		if string(conformance.Must(s.Get(ctx, "uploads/x")).Data) != "uploaded" {
			t.Fatal("PUT url did not upload")
		}
		h.Advance(ctx, 61*time.Second)
		_, err := h.Fetch(ctx, url)
		conformance.ExpectError(t, err, providererr.Denied, "")
		_, err = s.TemporaryURL(ctx, "k", 0, "GET")
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})

	// SignedUpload（STOR-SEC，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 8.2 节）：拿到地址的一方
	// （环境里的 agent，也就是用户代码）只能把内容正确、长度正确的这一个对象写一次。
	content := []byte("block \x00\xff content")
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	absent := func(t *testing.T, s objectstore.Store) {
		t.Helper()
		_, err := s.Head(ctx, "blocks/aa")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	}
	conformance.Case(t, "SignedUploadWritesTheSignedContent", func(t *testing.T) {
		h, s := setup(t)
		up := conformance.Must(s.SignedUpload(ctx, "blocks/aa", 5*time.Minute, digest, int64(len(content))))
		if up.URL == "" || len(up.Headers) == 0 {
			t.Fatalf("signed upload = %+v", up)
		}
		conformance.NoError(t, h.UploadSigned(ctx, up.URL, content, up.Headers))
		if got := conformance.Must(s.Get(ctx, "blocks/aa")).Data; string(got) != string(content) {
			t.Fatalf("got %q", got)
		}
	})
	conformance.Case(t, "SignedUploadRejectsOtherContent", func(t *testing.T) {
		for _, other := range []string{"BLOCK \x00\xff CONTENT", "block \x00\xff content!", "short"} {
			h, s := setup(t)
			up := conformance.Must(s.SignedUpload(ctx, "blocks/aa", 5*time.Minute, digest, int64(len(content))))
			if err := h.UploadSigned(ctx, up.URL, []byte(other), up.Headers); err == nil {
				t.Fatalf("content %q was accepted", other)
			}
			absent(t, s)
		}
	})
	conformance.Case(t, "SignedUploadNeedsItsHeaders", func(t *testing.T) {
		h, s := setup(t)
		up := conformance.Must(s.SignedUpload(ctx, "blocks/aa", 5*time.Minute, digest, int64(len(content))))
		if err := h.UploadSigned(ctx, up.URL, content, map[string]string{}); err == nil {
			t.Fatal("upload without the signed headers was accepted")
		}
		absent(t, s)
	})
	conformance.Case(t, "SignedUploadNeverOverwrites", func(t *testing.T) {
		h, s := setup(t)
		put(t, s, "blocks/aa", "original")
		up := conformance.Must(s.SignedUpload(ctx, "blocks/aa", 5*time.Minute, digest, int64(len(content))))
		if err := h.UploadSigned(ctx, up.URL, content, up.Headers); err == nil {
			t.Fatal("signed upload overwrote an existing object")
		}
		if got := conformance.Must(s.Get(ctx, "blocks/aa")).Data; string(got) != "original" {
			t.Fatalf("object changed to %q", got)
		}
	})
	conformance.Case(t, "SignedUploadExpires", func(t *testing.T) {
		h, s := setup(t)
		up := conformance.Must(s.SignedUpload(ctx, "blocks/aa", 5*time.Minute, digest, int64(len(content))))
		h.Advance(ctx, 6*time.Minute)
		conformance.ExpectError(t, h.UploadSigned(ctx, up.URL, content, up.Headers), providererr.Denied, "")
	})
	conformance.Case(t, "SignedUploadRejectsBadArguments", func(t *testing.T) {
		_, s := setup(t)
		for _, c := range []struct {
			sha  string
			size int64
			ttl  time.Duration
		}{
			{"not-hex", 1, time.Minute},
			{strings.Repeat("A", 64), 1, time.Minute},
			{strings.Repeat("a", 64), -1, time.Minute},
			{strings.Repeat("a", 64), 1, 0},
			{strings.Repeat("a", 64), 1, objectstore.MaxSignedUploadTTL + time.Second},
		} {
			_, err := s.SignedUpload(ctx, "blocks/aa", c.ttl, c.sha, c.size)
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "LifecycleExpiresOldObjects", func(t *testing.T) {
		h, s := setup(t)
		conformance.NoError(t, s.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "tmp/", ExpireAfter: time.Hour}}))
		put(t, s, "tmp/old", "x")
		put(t, s, "keep/old", "x")
		h.Advance(ctx, 2*time.Hour)
		put(t, s, "tmp/new", "x")
		if err := h.RunLifecycle(ctx); errors.Is(err, conformance.ErrUnsupported) {
			t.Skip("该实现无法立即执行生命周期清理")
		} else {
			conformance.NoError(t, err)
		}
		_, err := s.Get(ctx, "tmp/old")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		conformance.Must(s.Get(ctx, "keep/old"))
		conformance.Must(s.Get(ctx, "tmp/new"))
	})
}
