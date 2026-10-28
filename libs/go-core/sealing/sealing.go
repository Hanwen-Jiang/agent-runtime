// Package sealing 把只用一次的凭据封起来，跨服务交接时不以明文进事件历史（docs/plan/cloud-deploy.md D-06）。
// task-service 的 WorkspaceService 封，Worker 的工作区操作解（Python 的 agentruntime_core.sealing），两边对着同一组向量测试
// （tests/contract/sealed_credential_vectors.json）：
//
//	wsc1.<base64url(nonce(12) || AES-256-GCM 密文)>
//	密钥 = SHA-256(SecretStore 平台作用域的 workspace/credential-key)，关联数据 = "wsc1|" + tenant_id + "|" + operation_id
//
// 关联数据把密文绑在这个租户的这一次操作上，挪到别的操作或租户解不开。解不开一律是同一个 denied（invalid_sealed），不说明原因。
package sealing

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// Prefix 是格式的版本前缀。
const Prefix = "wsc1."

// SecretName 是密钥在 SecretStore 平台作用域里的名字。
const SecretName = "workspace/credential-key"

func aead(key []byte) (cipher.AEAD, error) {
	if len(key) < 32 {
		return nil, providererr.New(providererr.Invalid, "weak_sealing_key", "封凭据的密钥至少 32 字节")
	}
	sum := sha256.Sum256(key)
	block, err := aes.NewCipher(sum[:])
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func associated(tenantID, operationID string) []byte {
	return []byte("wsc1|" + tenantID + "|" + operationID)
}

// Seal 封起 plaintext。nonce 为 nil 时随机生成（只有测试向量传固定的）。
func Seal(key []byte, tenantID, operationID string, plaintext, nonce []byte) (string, error) {
	g, err := aead(key)
	if err != nil {
		return "", err
	}
	if nonce == nil {
		nonce = make([]byte, g.NonceSize())
		if _, err := rand.Read(nonce); err != nil {
			return "", err
		}
	}
	if len(nonce) != g.NonceSize() {
		return "", providererr.New(providererr.Invalid, "invalid_nonce", "nonce 要 12 字节")
	}
	out := g.Seal(append([]byte{}, nonce...), nonce, plaintext, associated(tenantID, operationID))
	return Prefix + base64.RawURLEncoding.EncodeToString(out), nil
}

// Open 解开；任何一项不对都是 denied（invalid_sealed）。
func Open(key []byte, tenantID, operationID, sealed string) ([]byte, error) {
	bad := providererr.New(providererr.Denied, "invalid_sealed", "封起的凭据无效")
	if !strings.HasPrefix(sealed, Prefix) {
		return nil, bad
	}
	g, err := aead(key)
	if err != nil {
		return nil, err
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(sealed, Prefix))
	if err != nil || len(raw) < g.NonceSize()+g.Overhead() {
		return nil, bad
	}
	plain, err := g.Open(nil, raw[:g.NonceSize()], raw[g.NonceSize():], associated(tenantID, operationID))
	if err != nil {
		return nil, bad
	}
	return plain, nil
}
