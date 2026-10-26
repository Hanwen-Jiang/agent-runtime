package server

import (
	"fmt"
	"net/http"
	"strings"
)

// 给用户看的文字按请求的语言写出，做法同 gettext：代码里写中文，中文原文同时是键，english 表（messages_en.go）按原文给出英文。
// 请求的语言取网页写的 cw_lang Cookie（OAuth 回调这类整页跳转也带着它）；没有时看 Accept-Language（zh、en 之外的语言用英文）；
// 连这个头也没有时用中文。日志与启动配置的错误只给运维看，不经这里。

const (
	langZH     = "zh"
	langEN     = "en"
	langCookie = "cw_lang"
)

func requestLang(r *http.Request) string {
	if c, err := r.Cookie(langCookie); err == nil && (c.Value == langZH || c.Value == langEN) {
		return c.Value
	}
	header := r.Header.Get("Accept-Language")
	if strings.TrimSpace(header) == "" {
		return langZH
	}
	for _, part := range strings.Split(header, ",") {
		tag, _, _ := strings.Cut(part, ";")
		tag = strings.ToLower(strings.TrimSpace(tag))
		switch {
		case strings.HasPrefix(tag, "zh"):
			return langZH
		case strings.HasPrefix(tag, "en"):
			return langEN
		}
	}
	return langEN
}

// Msg 是要给用户看的一句话：中文的格式串加参数。它也是 error，Error() 是中文（写日志用）。
type Msg struct {
	Format string
	Args   []any
}

// msg 造一句给用户看的话。format 必须是字符串字面量：测试按源码核对每一句都有英文。
func msg(format string, args ...any) *Msg { return &Msg{Format: format, Args: args} }

func (m *Msg) Error() string { return m.In(langZH) }

// In 按语言写出：英文按 english 表取格式串（表里没有时用中文）；参数里的 error 也按同一种语言写。
func (m *Msg) In(lang string) string {
	format := m.Format
	if lang == langEN {
		if en, ok := english[m.Format]; ok {
			format = en
		}
	}
	if len(m.Args) == 0 {
		return format
	}
	args := make([]any, len(m.Args))
	for i, a := range m.Args {
		if err, ok := a.(error); ok {
			args[i] = localize(lang, err)
		} else {
			args[i] = a
		}
	}
	return fmt.Sprintf(format, args...)
}

// localize 把错误按语言写成文字：*Msg 与 *GitError 按语言写；其余（系统、git、平台给的错误）原样。
func localize(lang string, err error) string {
	switch e := err.(type) {
	case *Msg:
		return e.In(lang)
	case *GitError:
		return e.In(lang)
	}
	return err.Error()
}

// writeMsg 写出一条错误说明（JSON 的 error 字段），按请求的语言。format 必须是字符串字面量。
func writeMsg(w http.ResponseWriter, r *http.Request, status int, format string, args ...any) {
	writeErr(w, r, status, msg(format, args...))
}

// writeErr 写出一个错误：*Msg、*GitError 按请求的语言，其余原样。
func writeErr(w http.ResponseWriter, r *http.Request, status int, err error) {
	writeJSON(w, status, map[string]string{"error": localize(requestLang(r), err)})
}
