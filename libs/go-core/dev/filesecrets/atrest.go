package filesecrets

// 静态加密（R-04，总表第 0 节 V1-SEC，2026-11-06）：密钥文件在共用卷上，原来是明文。给了主密钥（AR_SECRETS_MASTER_KEY_FILE，
// 32 字节，base64；来自只挂给要它的服务的 K8s Secret）时，文件里每个作用域的密钥值与 kek 各自用 keywrap.go 的 ARK1 包装成
// "arenc1:<base64>"：作用域与用途（"secret/<名字>"、"kek"）写进包装并参与认证，所以密文不能被挪到别的作用域或别的名字下用。
// 内存里仍是明文，接口不变。Python 侧（agentruntime_core.dev.secret_store）格式相同，两边互相读得懂。
//
// 上线分两步（滚动升级时新旧进程共用一份文件）：先让所有进程都带主密钥、读得懂密文但照旧写明文；
// 全部换完之后再设 AR_SECRETS_ENCRYPT=1，之后每次写文件都加密，旧的明文值在下一次写时变成密文。
// 没有主密钥却在文件里见到密文时拒绝启动（不当作明文用）。

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"os"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

const (
	// EnvMasterKeyFile 是主密钥文件（32 字节的 base64）。
	EnvMasterKeyFile = "AR_SECRETS_MASTER_KEY_FILE"
	// EnvEncrypt 为 "1" 时写文件加密（要有主密钥）。
	EnvEncrypt = "AR_SECRETS_ENCRYPT"
	encPrefix  = "arenc1:"
)

// atRest 是静态加密的设置；master 为空时文件照旧是明文。
type atRest struct {
	master  []byte
	encrypt bool
}

func atRestFromEnv() (atRest, error) {
	var a atRest
	if path := strings.TrimSpace(os.Getenv(EnvMasterKeyFile)); path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return a, providererr.New(providererr.Invalid, "invalid_master_key", "读不了密钥文件的主密钥（"+EnvMasterKeyFile+"）")
		}
		key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(raw)))
		if err != nil || len(key) != 32 {
			return a, providererr.New(providererr.Invalid, "invalid_master_key", "主密钥要是 32 字节的 base64")
		}
		a.master = key
	}
	a.encrypt = os.Getenv(EnvEncrypt) == "1"
	if a.encrypt && a.master == nil {
		return a, providererr.New(providererr.Invalid, "invalid_master_key", EnvEncrypt+"=1 要有主密钥（"+EnvMasterKeyFile+"）")
	}
	return a, nil
}

func secretPurpose(name string) string {
	p := "secret/" + name
	if len(p) > 255 { // ARK1 的用途字段一个字节记长度
		sum := sha256.Sum256([]byte(name))
		p = "secret#" + hex.EncodeToString(sum[:])
	}
	return p
}

func (a atRest) seal(owner, purpose, value string) string {
	return encPrefix + base64.StdEncoding.EncodeToString(wrap(a.master, owner, purpose, []byte(value)))
}

func (a atRest) open(owner, purpose, value string) (string, error) {
	if !strings.HasPrefix(value, encPrefix) {
		return value, nil // 明文（加密之前写的，或没打开加密）
	}
	if a.master == nil {
		return "", providererr.New(providererr.Invalid, "missing_master_key", "密钥文件里有加密的值，但没给主密钥（"+EnvMasterKeyFile+"）")
	}
	raw, err := base64.StdEncoding.DecodeString(value[len(encPrefix):])
	if err != nil {
		return "", providererr.New(providererr.Invalid, "invalid_sealed_secret", "密钥文件里加密的值格式不对")
	}
	plain, err := unwrap(a.master, owner, purpose, raw)
	if err != nil {
		return "", providererr.New(providererr.Invalid, "invalid_sealed_secret", "密钥文件里加密的值解不开（主密钥不对，或值被挪过）")
	}
	return string(plain), nil
}

// opened 把从文件读到的内容解成明文（就地）。
func (a atRest) opened(f *file) error {
	for owner, e := range f.Scopes {
		if e == nil {
			continue
		}
		for name, v := range e.Secrets {
			plain, err := a.open(owner, secretPurpose(name), v)
			if err != nil {
				return err
			}
			e.Secrets[name] = plain
		}
		if e.KEK != "" {
			plain, err := a.open(owner, "kek", e.KEK)
			if err != nil {
				return err
			}
			e.KEK = plain
		}
	}
	return nil
}

// sealed 返回要写进文件的一份：打开加密时各值都加密，否则就是原样。
func (a atRest) sealed(f file) file {
	if !a.encrypt {
		return f
	}
	out := file{Version: f.Version, Scopes: make(map[string]*scopeEntry, len(f.Scopes))}
	for owner, e := range f.Scopes {
		if e == nil {
			continue
		}
		c := &scopeEntry{Secrets: make(map[string]string, len(e.Secrets)), Roles: e.Roles}
		for name, v := range e.Secrets {
			c.Secrets[name] = a.seal(owner, secretPurpose(name), v)
		}
		if e.KEK != "" {
			c.KEK = a.seal(owner, "kek", e.KEK)
		}
		out.Scopes[owner] = c
	}
	return out
}
