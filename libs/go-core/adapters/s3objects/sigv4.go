package s3objects

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

// AWS Signature Version 4（只用标准库），与 Python 侧 agentruntime_core.adapters.sigv4 一致；测试用 AWS 文档里的例子核对。

const (
	algorithm = "AWS4-HMAC-SHA256"
	// UnsignedPayload 是预签名地址的载荷哈希。
	UnsignedPayload = "UNSIGNED-PAYLOAD"
)

// EmptySHA256 是空载荷的 sha256。
var EmptySHA256 = hashHex(nil)

func hashHex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func hmacSHA256(key []byte, msg string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(msg))
	return m.Sum(nil)
}

func signingKey(secretKey, date, region, service string) []byte {
	k := hmacSHA256([]byte("AWS4"+secretKey), date)
	k = hmacSHA256(k, region)
	k = hmacSHA256(k, service)
	return hmacSHA256(k, "aws4_request")
}

// Escape 按 SigV4 的规则编码：只保留 A-Z a-z 0-9 - _ . ~；path 为真时保留 /。
func Escape(s string, path bool) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		if 'A' <= c && c <= 'Z' || 'a' <= c && c <= 'z' || '0' <= c && c <= '9' || c == '-' || c == '_' || c == '.' || c == '~' || (path && c == '/') {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func canonicalQuery(raw string) string {
	values, _ := url.ParseQuery(raw)
	type pair struct{ k, v string }
	var pairs []pair
	for k, vs := range values {
		for _, v := range vs {
			pairs = append(pairs, pair{k, v})
		}
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].k != pairs[j].k {
			return pairs[i].k < pairs[j].k
		}
		return pairs[i].v < pairs[j].v
	})
	parts := make([]string, len(pairs))
	for i, p := range pairs {
		parts[i] = Escape(p.k, false) + "=" + Escape(p.v, false)
	}
	return strings.Join(parts, "&")
}

func canonicalRequest(method, path, rawQuery string, headers map[string]string, signed []string, payloadHash string) string {
	if path == "" {
		path = "/"
	}
	var lines strings.Builder
	for _, name := range signed {
		lines.WriteString(name + ":" + strings.Join(strings.Fields(headers[name]), " ") + "\n")
	}
	return strings.Join([]string{method, path, canonicalQuery(rawQuery), lines.String(), strings.Join(signed, ";"), payloadHash}, "\n")
}

func stamp(now time.Time) (string, string) {
	now = now.UTC()
	return now.Format("20060102T150405Z"), now.Format("20060102")
}

func signature(secretKey, date, region, service, amzDate, scope, request string) string {
	toSign := strings.Join([]string{algorithm, amzDate, scope, hashHex([]byte(request))}, "\n")
	return hex.EncodeToString(hmacSHA256(signingKey(secretKey, date, region, service), toSign))
}

// Sign 给请求签名：加上 x-amz-date、x-amz-content-sha256（s3）与 Authorization。签请求上已有的所有头与 host。
// 请求的路径要已经按 S3 的规则编码好（Escape），并且 URL.EscapedPath() 原样返回它。
func Sign(req *http.Request, payloadHash, accessKey string, secretKey secret.Value, region, service string, now time.Time) {
	amzDate, date := stamp(now)
	req.Header.Set("X-Amz-Date", amzDate)
	if service == "s3" {
		req.Header.Set("X-Amz-Content-Sha256", payloadHash)
	}
	host := req.Host
	if host == "" {
		host = req.URL.Host
	}
	headers := map[string]string{"host": host}
	for name, values := range req.Header {
		if strings.EqualFold(name, "Authorization") {
			continue
		}
		headers[strings.ToLower(name)] = strings.Join(values, ",")
	}
	signed := make([]string, 0, len(headers))
	for name := range headers {
		signed = append(signed, name)
	}
	sort.Strings(signed)
	request := canonicalRequest(req.Method, req.URL.EscapedPath(), req.URL.RawQuery, headers, signed, payloadHash)
	scope := date + "/" + region + "/" + service + "/aws4_request"
	sig := signature(secretKey.RevealString(), date, region, service, amzDate, scope, request)
	req.Header.Set("Authorization", algorithm+" Credential="+accessKey+"/"+scope+", SignedHeaders="+strings.Join(signed, ";")+", Signature="+sig)
}

// Presign 生成预签名地址（查询串里带签名）：只签 host 头，载荷不签。
func Presign(method, rawURL string, expires int, accessKey string, secretKey secret.Value, region, service string, now time.Time) (string, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", err
	}
	amzDate, date := stamp(now)
	scope := date + "/" + region + "/" + service + "/aws4_request"
	params := [][2]string{
		{"X-Amz-Algorithm", algorithm},
		{"X-Amz-Credential", accessKey + "/" + scope},
		{"X-Amz-Date", amzDate},
		{"X-Amz-Expires", strconv.Itoa(expires)},
		{"X-Amz-SignedHeaders", "host"},
	}
	parts := []string{}
	if u.RawQuery != "" {
		parts = append(parts, u.RawQuery)
	}
	for _, p := range params {
		parts = append(parts, Escape(p[0], false)+"="+Escape(p[1], false))
	}
	query := strings.Join(parts, "&")
	request := canonicalRequest(method, u.EscapedPath(), query, map[string]string{"host": u.Host}, []string{"host"}, UnsignedPayload)
	u.RawQuery = query + "&X-Amz-Signature=" + signature(secretKey.RevealString(), date, region, service, amzDate, scope, request)
	return u.String(), nil
}
