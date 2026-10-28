package sealing

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// 与 Python 的 agentruntime_core.sealing 共用一份向量（tests/contract/sealed_credential_vectors.json）。

type vector struct {
	Name        string `json:"name"`
	Key         string `json:"key"`
	TenantID    string `json:"tenant_id"`
	OperationID string `json:"operation_id"`
	Plaintext   string `json:"plaintext"`
	NonceHex    string `json:"nonce_hex"`
	Sealed      string `json:"sealed"`
}

type tampered struct {
	Name        string  `json:"name"`
	From        string  `json:"from"`
	Key         *string `json:"key"`
	TenantID    *string `json:"tenant_id"`
	OperationID *string `json:"operation_id"`
	Sealed      *string `json:"sealed"`
	Flip        *int    `json:"flip"`
}

type vectors struct {
	Valid    []vector   `json:"valid"`
	Tampered []tampered `json:"tampered"`
}

func load(t *testing.T) vectors {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(file), "..", "..", "..", "tests", "contract", "sealed_credential_vectors.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读不到向量：%v", err)
	}
	var v vectors
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("向量格式不对：%v", err)
	}
	return v
}

func TestValidVectorsSealAndOpen(t *testing.T) {
	for _, c := range load(t).Valid {
		nonce, _ := hex.DecodeString(c.NonceHex)
		got, err := Seal([]byte(c.Key), c.TenantID, c.OperationID, []byte(c.Plaintext), nonce)
		if err != nil || got != c.Sealed {
			t.Fatalf("%s：Seal 得到 %q（%v），要 %q", c.Name, got, err, c.Sealed)
		}
		plain, err := Open([]byte(c.Key), c.TenantID, c.OperationID, c.Sealed)
		if err != nil || string(plain) != c.Plaintext {
			t.Fatalf("%s：Open 得到 %q（%v）", c.Name, plain, err)
		}
	}
}

func TestTamperedOrMovedCredentialsAreDenied(t *testing.T) {
	v := load(t)
	byName := map[string]vector{}
	for _, c := range v.Valid {
		byName[c.Name] = c
	}
	for _, c := range v.Tampered {
		base := byName[c.From]
		key, tenant, op, sealed := base.Key, base.TenantID, base.OperationID, base.Sealed
		if c.Key != nil {
			key = *c.Key
		}
		if c.TenantID != nil {
			tenant = *c.TenantID
		}
		if c.OperationID != nil {
			op = *c.OperationID
		}
		if c.Sealed != nil {
			sealed = *c.Sealed
		}
		if c.Flip != nil {
			raw, _ := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(sealed, Prefix))
			raw[*c.Flip] ^= 0x01
			sealed = Prefix + base64.RawURLEncoding.EncodeToString(raw)
		}
		_, err := Open([]byte(key), tenant, op, sealed)
		if providererr.CategoryOf(err) != providererr.Denied || strings.Contains(err.Error(), base.Plaintext) {
			t.Fatalf("%s：要 denied 且不带明文，得到 %v", c.Name, err)
		}
	}
}

func TestRandomNoncesAndWeakKeys(t *testing.T) {
	key := []byte(strings.Repeat("k", 32))
	a, _ := Seal(key, "t1", "wso-1", []byte("x"), nil)
	b, _ := Seal(key, "t1", "wso-1", []byte("x"), nil)
	if a == b {
		t.Fatal("每次封的 nonce 要不同")
	}
	if _, err := Seal([]byte("short"), "t1", "wso-1", []byte("x"), nil); providererr.CategoryOf(err) != providererr.Invalid {
		t.Fatalf("弱密钥要 invalid：%v", err)
	}
}
