package s3objects

// 测试用的 S3 兼容服务（与 Python 侧 agentruntime_core.adapters.s3test 对应）：只实现适配器用到的部分，
// 并按收到的原始请求（RequestURI 与头）重算 SigV4 签名、核对预签名地址的过期时间（时间取注入的时钟）。

import (
	"bytes"
	"crypto/md5"
	"encoding/base64"
	"encoding/xml"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

var fakeAuth = regexp.MustCompile(`^AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, SignedHeaders=([^,]+), Signature=([0-9a-f]{64})$`)

type fakeObject struct {
	data     []byte
	header   http.Header
	modified time.Time
}

// FakeS3 是测试里的 S3 兼容服务。
type FakeS3 struct {
	URL                    string
	Bucket, Access, Secret string
	Now                    func() time.Time

	mu        sync.Mutex
	objects   map[string]fakeObject
	lifecycle []byte
}

// StartFakeS3 启动一个假服务，测试结束时关闭。
func StartFakeS3(t *testing.T, bucket, access, secretKey string, now func() time.Time) *FakeS3 {
	f := &FakeS3{Bucket: bucket, Access: access, Secret: secretKey, Now: now, objects: map[string]fakeObject{}}
	srv := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(srv.Close)
	f.URL = srv.URL
	return f
}

// Names 是桶里所有对象名（排好序）。
func (f *FakeS3) Names() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for n := range f.objects {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// Lifecycle 是当前的生命周期配置；没有时为 nil。
func (f *FakeS3) Lifecycle() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lifecycle
}

// RunLifecycle 按生命周期配置清理一次（Expiration/Days，按整天算）。
func (f *FakeS3) RunLifecycle() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.lifecycle == nil {
		return 0
	}
	var cfg struct {
		Rules []struct {
			Prefix string `xml:"Filter>Prefix"`
			Status string `xml:"Status"`
			Days   int    `xml:"Expiration>Days"`
		} `xml:"Rule"`
	}
	if xml.Unmarshal(f.lifecycle, &cfg) != nil {
		return 0
	}
	now, gone := f.Now(), 0
	for name, obj := range f.objects {
		for _, r := range cfg.Rules {
			if r.Status == "Enabled" && strings.HasPrefix(name, r.Prefix) && !obj.modified.Add(time.Duration(r.Days)*24*time.Hour).After(now) {
				delete(f.objects, name)
				gone++
				break
			}
		}
	}
	return gone
}

func (f *FakeS3) verify(r *http.Request, body []byte) string {
	path, rawQuery, _ := strings.Cut(r.RequestURI, "?")
	q, _ := url.ParseQuery(rawQuery)
	headers := map[string]string{"host": r.Host}
	for name, values := range r.Header {
		headers[strings.ToLower(name)] = strings.Join(values, ",")
	}
	var access, date, amzDate, want, payload string
	var signed []string
	if sig := q.Get("X-Amz-Signature"); sig != "" {
		when, err := time.Parse("20060102T150405Z", q.Get("X-Amz-Date"))
		expires, _ := strconv.Atoi(q.Get("X-Amz-Expires"))
		if err != nil || f.Now().After(when.Add(time.Duration(expires)*time.Second)) {
			return "AccessDenied"
		}
		cred := strings.Split(q.Get("X-Amz-Credential"), "/")
		if len(cred) < 2 {
			return "AccessDenied"
		}
		access, date, amzDate, want, payload = cred[0], cred[1], q.Get("X-Amz-Date"), sig, UnsignedPayload
		signed = strings.Split(q.Get("X-Amz-SignedHeaders"), ";")
		q.Del("X-Amz-Signature")
		rawQuery = q.Encode()
	} else {
		m := fakeAuth.FindStringSubmatch(r.Header.Get("Authorization"))
		if m == nil {
			return "AccessDenied"
		}
		access, date, want = m[1], m[2], m[5]
		signed = strings.Split(m[4], ";")
		payload = r.Header.Get("X-Amz-Content-Sha256")
		if payload != hashHex(body) {
			return "XAmzContentSHA256Mismatch"
		}
		amzDate = r.Header.Get("X-Amz-Date")
		when, err := time.Parse("20060102T150405Z", amzDate)
		if err != nil || f.Now().Sub(when).Abs() > 15*time.Minute {
			return "RequestTimeTooSkewed"
		}
	}
	if access != f.Access {
		return "InvalidAccessKeyId"
	}
	request := canonicalRequest(r.Method, path, rawQuery, headers, signed, payload)
	if signature(f.Secret, date, "auto", "s3", amzDate, date+"/auto/s3/aws4_request", request) != want {
		return "SignatureDoesNotMatch"
	}
	return ""
}

func (f *FakeS3) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	if problem := f.verify(r, body); problem != "" {
		f.fail(w, http.StatusForbidden, problem)
		return
	}
	bucket, name, _ := strings.Cut(strings.TrimPrefix(r.URL.Path, "/"), "/")
	if bucket != f.Bucket {
		f.fail(w, http.StatusNotFound, "NoSuchBucket")
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	q := r.URL.Query()
	if name == "" {
		switch {
		case q.Has("lifecycle"):
			f.serveLifecycle(w, r, body)
		case r.Method == http.MethodGet && q.Get("list-type") == "2":
			f.serveList(w, q)
		default:
			f.fail(w, http.StatusBadRequest, "InvalidRequest")
		}
		return
	}
	obj, ok := f.objects[name]
	switch r.Method {
	case http.MethodPut:
		if r.Header.Get("If-None-Match") == "*" && ok {
			f.fail(w, http.StatusPreconditionFailed, "PreconditionFailed")
			return
		}
		kept := http.Header{}
		for k, v := range r.Header {
			if strings.HasPrefix(strings.ToLower(k), "x-amz-meta-") || strings.EqualFold(k, "Content-Type") {
				kept[k] = v
			}
		}
		f.objects[name] = fakeObject{data: body, header: kept, modified: f.Now()}
		w.WriteHeader(http.StatusOK)
	case http.MethodDelete:
		delete(f.objects, name)
		w.WriteHeader(http.StatusNoContent)
	case http.MethodGet, http.MethodHead:
		if !ok {
			f.fail(w, http.StatusNotFound, "NoSuchKey")
			return
		}
		for k, v := range obj.header {
			w.Header()[k] = v
		}
		w.Header().Set("Last-Modified", obj.modified.UTC().Format(http.TimeFormat))
		w.Header().Set("Content-Length", strconv.Itoa(len(obj.data)))
		w.WriteHeader(http.StatusOK)
		if r.Method == http.MethodGet {
			_, _ = w.Write(obj.data)
		}
	default:
		f.fail(w, http.StatusMethodNotAllowed, "MethodNotAllowed")
	}
}

func (f *FakeS3) serveList(w http.ResponseWriter, q url.Values) {
	prefix := q.Get("prefix")
	limit, _ := strconv.Atoi(q.Get("max-keys"))
	if limit <= 0 {
		limit = 1000
	}
	after := ""
	if token := q.Get("continuation-token"); token != "" {
		raw, _ := base64.URLEncoding.DecodeString(token)
		after = string(raw)
	}
	var names []string
	for n := range f.objects {
		if strings.HasPrefix(n, prefix) && n > after {
			names = append(names, n)
		}
	}
	sort.Strings(names)
	truncated := len(names) > limit
	if truncated {
		names = names[:limit]
	}
	var b bytes.Buffer
	fmt.Fprintf(&b, `<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="%s"><Name>%s</Name><IsTruncated>%t</IsTruncated>`, s3NS, f.Bucket, truncated)
	if truncated {
		fmt.Fprintf(&b, "<NextContinuationToken>%s</NextContinuationToken>", base64.URLEncoding.EncodeToString([]byte(names[len(names)-1])))
	}
	for _, n := range names {
		fmt.Fprintf(&b, "<Contents><Key>%s</Key><Size>%d</Size></Contents>", html.EscapeString(n), len(f.objects[n].data))
	}
	b.WriteString("</ListBucketResult>")
	w.Header().Set("Content-Type", "application/xml")
	_, _ = w.Write(b.Bytes())
}

func (f *FakeS3) serveLifecycle(w http.ResponseWriter, r *http.Request, body []byte) {
	switch r.Method {
	case http.MethodGet:
		if f.lifecycle == nil {
			f.fail(w, http.StatusNotFound, "NoSuchLifecycleConfiguration")
			return
		}
		_, _ = w.Write(f.lifecycle)
	case http.MethodPut:
		sum := md5.Sum(body)
		if r.Header.Get("Content-MD5") != base64.StdEncoding.EncodeToString(sum[:]) {
			f.fail(w, http.StatusBadRequest, "InvalidDigest")
			return
		}
		f.lifecycle = body
	case http.MethodDelete:
		f.lifecycle = nil
		w.WriteHeader(http.StatusNoContent)
	default:
		f.fail(w, http.StatusMethodNotAllowed, "MethodNotAllowed")
	}
}

func (f *FakeS3) fail(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/xml")
	w.WriteHeader(status)
	fmt.Fprintf(w, `<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code><Message>%s</Message></Error>`, code, code)
}
