package server

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/products/coding/internal/samples"
)

// sourceMessages 按源码找出全部给用户看的中文格式串：msg 的第一个参数、writeMsg 的第四个参数。
// 它们必须是字符串字面量，否则抽不出来、也就核对不了有没有英文。
func sourceMessages(t *testing.T) map[string]string {
	t.Helper()
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]string{} // 格式串 → 第一次出现的位置
	fset := token.NewFileSet()
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") || name == "i18n.go" { // i18n.go 只定义 msg 与 writeMsg
			continue
		}
		f, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			fn, ok := call.Fun.(*ast.Ident)
			if !ok {
				return true
			}
			at := map[string]int{"msg": 0, "writeMsg": 3}[fn.Name]
			if (fn.Name != "msg" && fn.Name != "writeMsg") || len(call.Args) <= at {
				return true
			}
			lit, ok := call.Args[at].(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				t.Errorf("%s：%s 的格式串要写成字符串字面量", fset.Position(call.Pos()), fn.Name)
				return true
			}
			s, _ := strconv.Unquote(lit.Value)
			if _, seen := out[s]; !seen {
				out[s] = fset.Position(call.Pos()).String()
			}
			return true
		})
	}
	return out
}

// argCount 是格式串要几个参数：从 0 个试起，直到 fmt 不再报缺参数或多参数。
func argCount(format string) int {
	for n := 0; n <= 6; n++ {
		if s := fmt.Sprintf(format, placeholders(n)...); !strings.Contains(s, "%!") {
			return n
		}
	}
	return -1
}

func placeholders(n int) []any {
	out := make([]any, n)
	for i := range out {
		out[i] = fmt.Sprintf("<arg%d>", i+1)
	}
	return out
}

// 每一句给用户看的中文都有英文；参数个数一样、每个参数都用上了；english 里没有源码用不到的句子。
func TestEveryMessageHasEnglish(t *testing.T) {
	found := sourceMessages(t)
	if len(found) < 50 {
		t.Fatalf("只找到 %d 句，抽取多半坏了", len(found))
	}
	keys := make([]string, 0, len(found))
	for k := range found {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	for _, zh := range keys {
		en, ok := english[zh]
		if !ok {
			t.Errorf("%s：缺英文：%q", found[zh], zh)
			continue
		}
		n := argCount(zh)
		if n < 0 {
			t.Errorf("%s：中文格式串不对：%q", found[zh], zh)
			continue
		}
		out := fmt.Sprintf(en, placeholders(n)...)
		if strings.Contains(out, "%!") {
			t.Errorf("%q 的英文参数对不上（要 %d 个）：%q", zh, n, out)
		}
		for i := 1; i <= n; i++ {
			if !strings.Contains(out, fmt.Sprintf("<arg%d>", i)) {
				t.Errorf("%q 的英文没用上第 %d 个参数：%q", zh, i, en)
			}
		}
		if strings.ContainsFunc(en, func(r rune) bool { return r >= 0x4e00 && r <= 0x9fff }) {
			t.Errorf("%q 的英文里还有中文：%q", zh, en)
		}
	}
	for zh := range english {
		if _, ok := found[zh]; !ok {
			t.Errorf("english 里有源码用不到的句子：%q", zh)
		}
	}
}

func TestRequestLanguage(t *testing.T) {
	cases := []struct {
		cookie, accept, want string
	}{
		{"", "", langZH},               // 什么都没有：中文
		{"", "en-US,en;q=0.9", langEN}, // 按浏览器
		{"", "zh-CN,zh;q=0.9,en;q=0.8", langZH},
		{"", "fr-FR,fr;q=0.9,en;q=0.8", langEN}, // 先认得的那个
		{"", "fr-FR", langEN},                   // 都不认得：英文（同网页）
		{"en", "zh-CN", langEN},                 // 网页选的语言优先
		{"zh", "en-US", langZH},
		{"de", "zh-CN", langZH}, // Cookie 不认得时看浏览器
	}
	for _, tc := range cases {
		r := httptest.NewRequest(http.MethodGet, "/", nil)
		if tc.cookie != "" {
			r.AddCookie(&http.Cookie{Name: langCookie, Value: tc.cookie})
		}
		if tc.accept != "" {
			r.Header.Set("Accept-Language", tc.accept)
		}
		if got := requestLang(r); got != tc.want {
			t.Errorf("Cookie %q、Accept-Language %q：应为 %s，实际 %s", tc.cookie, tc.accept, tc.want, got)
		}
	}
}

// 套着的错误按同一种语言写；系统与平台给的错误原样。
func TestMessagesLocalizeTheirArguments(t *testing.T) {
	m := msg("建项目失败：%v", msg("路径不合法"))
	if got := m.In(langEN); got != "Couldn't create the workspace: Invalid path" {
		t.Fatalf("英文：%q", got)
	}
	if got := m.Error(); got != "建项目失败：路径不合法" {
		t.Fatalf("Error() 是中文：%q", got)
	}
	ge := &GitError{Provider: "GitLab", Status: 401, Message: "401 Unauthorized"}
	if got := msg("授权过期且续不上：%v", ge).In(langEN); got != "Authorization expired and couldn't be renewed: GitLab 401: 401 Unauthorized" {
		t.Fatalf("平台的说明原样：%q", got)
	}
	note := &GitError{Provider: "Google", Status: 403, Note: msg("这个 Google 账号的邮箱还没有验证")}
	if got := localize(langEN, note); got != "Google 403: This Google account's email address isn't verified yet" {
		t.Fatalf("我们自己的说明按语言：%q", got)
	}
}

// 接口的错误按 cw_lang Cookie 写；没有 Cookie 时按 Accept-Language。
func TestAPIErrorsFollowTheLanguage(t *testing.T) {
	ts, _, _ := newServer(t)
	get := func(header http.Header) map[string]string {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/projects", nil)
		req.Header = header
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var body map[string]string
		_ = json.NewDecoder(res.Body).Decode(&body)
		return body
	}
	if body := get(http.Header{}); body["error"] != "请先登录" {
		t.Fatalf("默认中文：%v", body)
	}
	if body := get(http.Header{"Cookie": {"cw_lang=en"}}); body["error"] != "Please sign in first" {
		t.Fatalf("Cookie 选了英文：%v", body)
	}
	if body := get(http.Header{"Accept-Language": {"en-GB,en;q=0.9"}}); body["error"] != "Please sign in first" {
		t.Fatalf("浏览器是英文：%v", body)
	}

	c := newClient(t, ts.URL)
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	c.http.Jar.SetCookies(mustURL(t, ts.URL), []*http.Cookie{{Name: langCookie, Value: langEN, Path: "/"}})
	if code, body, _ := c.do("POST", "/api/projects", map[string]string{"name": "x", "git_url": "http://example.com/a.git"}); code != http.StatusBadRequest || !strings.HasPrefix(body["error"].(string), "Only https Git URLs") {
		t.Fatalf("登录后的接口也按 Cookie：%d %v", code, body)
	}
}

// OAuth 回调带回的错误按 Cookie 的语言写（整页跳转也带着 Cookie）。
func TestOAuthCallbackErrorsFollowTheLanguage(t *testing.T) {
	gh := newFakeGitHub(t)
	ts, _, srv := newOAuthServer(t, "github")
	pointProvider(srv, "github", gh.server.URL)
	c := newClient(t, ts.URL)
	c.http.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	for _, tc := range []struct{ lang, want string }{{"zh", "登录状态不匹配，请重新登录"}, {"en", "Sign-in state doesn't match. Please sign in again."}} {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/auth/github/callback?code=good-code&state=forged", nil)
		req.AddCookie(&http.Cookie{Name: langCookie, Value: tc.lang})
		res, err := c.http.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		loc := res.Header.Get("Location")
		_, query, _ := strings.Cut(loc, "?")
		q, _ := url.ParseQuery(query)
		if q.Get("error") != tc.want {
			t.Fatalf("%s：%s", tc.lang, loc)
		}
	}
}

// 示例仓库的名字、说明与起手的任务按语言给；每个示例都有中文与英文。
func TestSamplesFollowTheLanguage(t *testing.T) {
	for _, s := range samples.All {
		for _, lang := range []string{langZH, langEN} {
			if tx, ok := s.Text[lang]; !ok || tx.Name == "" || tx.Description == "" || tx.Prompt == "" {
				t.Fatalf("示例 %s 缺 %s 的文字", s.ID, lang)
			}
		}
	}
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	read := func() []map[string]string {
		_, _, raw := c.do("GET", "/api/samples", nil)
		var out []map[string]string
		if err := json.Unmarshal([]byte(raw), &out); err != nil || len(out) == 0 {
			t.Fatalf("示例列表：%s", raw)
		}
		return out
	}
	if got := read()[0]; got["id"] != "ci_rounding" || got["name"] != "CI 修复示例（Python）" || got["prompt"] == "" {
		t.Fatalf("默认中文：%v", got)
	}
	c.http.Jar.SetCookies(mustURL(t, ts.URL), []*http.Cookie{{Name: langCookie, Value: langEN, Path: "/"}})
	if got := read()[0]; got["name"] != "CI fix sample (Python)" || !strings.Contains(got["prompt"], "python -m unittest") {
		t.Fatalf("英文：%v", got)
	}
}

func mustURL(t *testing.T, raw string) *url.URL {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}
