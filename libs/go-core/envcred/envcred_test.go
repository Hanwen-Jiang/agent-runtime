package envcred_test

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// 与 Python 的 agentruntime_core.envcred 用同一份向量（Python 签发）。
type vectors struct {
	KeyHex  string `json:"key_hex"`
	NowUnix int64  `json:"now_unix"`
	Valid   []struct {
		Name    string         `json:"name"`
		Token   string         `json:"token"`
		Purpose string         `json:"purpose"`
		Claims  envcred.Claims `json:"claims"`
	} `json:"valid"`
	Invalid []struct {
		Name    string `json:"name"`
		Token   string `json:"token"`
		Purpose string `json:"purpose"`
	} `json:"invalid"`
}

func load(t *testing.T) (vectors, []byte, time.Time) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "tests", "contract", "sandbox_credential_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	key, err := hex.DecodeString(v.KeyHex)
	if err != nil {
		t.Fatal(err)
	}
	return v, key, time.Unix(v.NowUnix, 0)
}

func TestPythonIssuedVectorsVerify(t *testing.T) {
	v, key, now := load(t)
	for _, c := range v.Valid {
		got, err := envcred.Verify(key, c.Token, c.Purpose, now)
		if err != nil {
			t.Fatalf("%s：%v", c.Name, err)
		}
		if !reflect.DeepEqual(got, c.Claims) {
			t.Fatalf("%s：得到 %+v，应为 %+v", c.Name, got, c.Claims)
		}
	}
}

func TestInvalidVectorsAreDeniedWithoutSayingWhy(t *testing.T) {
	v, key, now := load(t)
	for _, c := range v.Invalid {
		_, err := envcred.Verify(key, c.Token, c.Purpose, now)
		if providererr.CategoryOf(err) != providererr.Denied {
			t.Fatalf("%s：应为 denied，得到 %v", c.Name, err)
		}
		if strings.Contains(err.Error(), c.Token) {
			t.Fatalf("%s：错误信息里有凭据", c.Name)
		}
	}
}

func TestIssueRoundTripsAndRefusesBadInput(t *testing.T) {
	_, key, now := load(t)
	c := envcred.Claims{EnvRef: "sbe-0123456789abcdef", TenantID: "t-a", ExpiresAt: now.Unix() + 60, Purpose: envcred.PurposeEgress}
	token, err := envcred.Issue(key, c)
	if err != nil {
		t.Fatal(err)
	}
	got, err := envcred.Verify(key, token, envcred.PurposeEgress, now)
	if err != nil || !reflect.DeepEqual(got, c) {
		t.Fatalf("往返不对：%+v %v", got, err)
	}
	if _, err := envcred.Issue([]byte("short"), c); err == nil {
		t.Fatal("短密钥应该被拒")
	}
	bad := c
	bad.Purpose = "anything"
	if _, err := envcred.Issue(key, bad); err == nil {
		t.Fatal("不认识的用途应该被拒")
	}
}
