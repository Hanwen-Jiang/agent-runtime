package filesecrets

// 开发用的数据密钥包装格式，与 Python 侧 libs/py-core/src/agentruntime_core/dev/_keywrap.py 相同：
//
//	"ARK1" | u8 作用域长度 | 作用域 | u8 用途长度 | 用途 | 16 字节 nonce | u16 密文长度（大端）| 密文 | 32 字节 tag
//	enc_key = HMAC(kek, "agentruntime/dev-keywrap/enc")，mac_key = HMAC(kek, "agentruntime/dev-keywrap/mac")
//	密钥流第 i 块 = HMAC(enc_key, nonce | u32(i) 大端)；tag = HMAC(mac_key, tag 之前的全部字节)
//
// 只用标准库，先加密后认证。生产环境用 Vault/OpenBao 的 transit 或云 KMS。

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"unicode/utf8"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

var magic = []byte("ARK1")

func derive(kek []byte, label string) []byte {
	mac := hmac.New(sha256.New, kek)
	mac.Write([]byte("agentruntime/dev-keywrap/" + label))
	return mac.Sum(nil)
}

func keystream(encKey, nonce []byte, n int) []byte {
	var out []byte
	for counter := uint32(0); len(out) < n; counter++ {
		mac := hmac.New(sha256.New, encKey)
		mac.Write(nonce)
		_ = binary.Write(mac, binary.BigEndian, counter)
		out = append(out, mac.Sum(nil)...)
	}
	return out[:n]
}

func xor(a, b []byte) []byte {
	out := make([]byte, len(a))
	for i := range a {
		out[i] = a[i] ^ b[i]
	}
	return out
}

func wrap(kek []byte, scope, purpose string, plaintext []byte) []byte {
	nonce := make([]byte, 16)
	_, _ = rand.Read(nonce)
	ct := xor(plaintext, keystream(derive(kek, "enc"), nonce, len(plaintext)))
	body := append([]byte{}, magic...)
	body = append(body, byte(len(scope)))
	body = append(body, scope...)
	body = append(body, byte(len(purpose)))
	body = append(body, purpose...)
	body = append(body, nonce...)
	body = binary.BigEndian.AppendUint16(body, uint16(len(ct)))
	body = append(body, ct...)
	mac := hmac.New(sha256.New, derive(kek, "mac"))
	mac.Write(body)
	return mac.Sum(body)
}

type parsed struct {
	scope, purpose  string
	nonce, ct, body []byte
	tag             []byte
}

func parse(w []byte) (parsed, error) {
	bad := providererr.New(providererr.Invalid, "invalid_wrapped_key", "包装后的数据密钥格式不对")
	if len(w) < 4 || string(w[:4]) != string(magic) {
		return parsed{}, bad
	}
	pos := 4
	field := func() (string, bool) {
		if pos >= len(w) {
			return "", false
		}
		n := int(w[pos])
		if pos+1+n > len(w) {
			return "", false
		}
		s := w[pos+1 : pos+1+n]
		pos += 1 + n
		return string(s), utf8.Valid(s)
	}
	scope, ok1 := field()
	purpose, ok2 := field()
	if !ok1 || !ok2 || pos+18 > len(w) {
		return parsed{}, bad
	}
	nonce := w[pos : pos+16]
	ctLen := int(binary.BigEndian.Uint16(w[pos+16 : pos+18]))
	pos += 18
	if len(w) != pos+ctLen+32 {
		return parsed{}, bad
	}
	return parsed{scope: scope, purpose: purpose, nonce: nonce, ct: w[pos : pos+ctLen], body: w[:pos+ctLen], tag: w[pos+ctLen:]}, nil
}

func unwrap(kek []byte, scope, purpose string, w []byte) ([]byte, error) {
	p, err := parse(w)
	if err != nil {
		return nil, err
	}
	if p.scope != scope {
		return nil, providererr.New(providererr.Denied, "cross_scope_key", "数据密钥属于别的作用域")
	}
	if p.purpose != purpose {
		return nil, providererr.New(providererr.Denied, "purpose_mismatch", "数据密钥的用途不符")
	}
	mac := hmac.New(sha256.New, derive(kek, "mac"))
	mac.Write(p.body)
	if !hmac.Equal(mac.Sum(nil), p.tag) {
		return nil, providererr.New(providererr.Invalid, "invalid_wrapped_key", "包装后的数据密钥校验失败")
	}
	return xor(p.ct, keystream(derive(kek, "enc"), p.nonce, len(p.ct))), nil
}
