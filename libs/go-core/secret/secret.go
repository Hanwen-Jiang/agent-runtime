// Package secret 提供凭据与密钥的值类型（CLAUDE.md：凭据与密钥不得出现在日志、trace 和错误信息里）。
//
// Python 与 Go 共用（Python 侧见 libs/py-core/src/agentruntime_core/secrets.py），两边语义一致：
// 打印、格式化、序列化都只得到掩码，取明文必须显式调用 Reveal。
//
// 明文放在一个未导出类型的指针后面：即使 Value 被放进别的结构体的未导出字段、再整体用 %+v 打印，
// fmt 也只会打出指针地址，不会打出内容。
package secret

import (
	"crypto/subtle"
	"errors"
	"fmt"
	"io"
	"log/slog"
)

// Mask 是所有打印形式的输出。
const Mask = "***"

var errNoSerialize = errors.New("secret.Value 不能被序列化")

type payload struct{ b []byte }

// Value 是一个密钥值。零值表示空。
type Value struct{ p *payload }

// New 用字符串构造。
func New(s string) Value { return Value{p: &payload{b: []byte(s)}} }

// FromBytes 用字节构造，会复制输入。
func FromBytes(b []byte) Value { return Value{p: &payload{b: append([]byte(nil), b...)}} }

// Reveal 返回明文的副本。
func (v Value) Reveal() []byte {
	if v.p == nil {
		return nil
	}
	return append([]byte(nil), v.p.b...)
}

// RevealString 以字符串返回明文。
func (v Value) RevealString() string { return string(v.Reveal()) }

// IsZero 判断是否为空。
func (v Value) IsZero() bool { return v.p == nil || len(v.p.b) == 0 }

// Equal 以常数时间比较。
func (v Value) Equal(o Value) bool { return subtle.ConstantTimeCompare(v.Reveal(), o.Reveal()) == 1 }

func (v Value) String() string   { return Mask }
func (v Value) GoString() string { return "secret.Value(" + Mask + ")" }

// Format 让所有格式动词（%v %+v %#v %s %q %x 等）都只输出掩码。
func (v Value) Format(f fmt.State, _ rune) { _, _ = io.WriteString(f, Mask) }

// LogValue 让 slog 只记录掩码。
func (v Value) LogValue() slog.Value { return slog.StringValue(Mask) }

func (v Value) MarshalJSON() ([]byte, error) { return nil, errNoSerialize }
func (v Value) MarshalText() ([]byte, error) { return nil, errNoSerialize }
