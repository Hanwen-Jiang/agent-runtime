package secret_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

const plain = "sk-live-0123456789"

type holder struct {
	Public  secret.Value
	private secret.Value
}

func TestEveryRenderingIsMasked(t *testing.T) {
	v := secret.New(plain)
	h := holder{Public: v, private: v}
	renders := []string{
		v.String(), v.GoString(),
		fmt.Sprintf("%v", v), fmt.Sprintf("%+v", v), fmt.Sprintf("%#v", v), fmt.Sprintf("%s", v),
		fmt.Sprintf("%q", v), fmt.Sprintf("%x", v), fmt.Sprintf("%X", v), fmt.Sprint(v),
		fmt.Sprintf("%v", h), fmt.Sprintf("%+v", h), fmt.Sprintf("%#v", h), fmt.Sprintf("%v", &h),
	}
	for _, text := range renders {
		if strings.Contains(text, plain) || strings.Contains(text, fmt.Sprintf("%x", []byte(plain))) {
			t.Errorf("plain text leaked: %q", text)
		}
	}
}

func TestSlogIsMasked(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, nil))
	logger.Info("calling tool", "credential", secret.New(plain))
	if strings.Contains(buf.String(), plain) {
		t.Fatalf("slog leaked: %s", buf.String())
	}
}

func TestCannotBeSerialized(t *testing.T) {
	if _, err := json.Marshal(map[string]any{"k": secret.New(plain)}); err == nil {
		t.Fatal("json.Marshal must fail")
	}
}

func TestRevealAndEqual(t *testing.T) {
	v := secret.New(plain)
	if v.RevealString() != plain || !v.Equal(secret.New(plain)) || v.Equal(secret.New("other")) {
		t.Fatal("Reveal/Equal broken")
	}
	b := []byte("raw")
	w := secret.FromBytes(b)
	b[0] = 'X'
	if w.RevealString() != "raw" {
		t.Fatal("FromBytes must copy its input")
	}
	if !(secret.Value{}).IsZero() {
		t.Fatal("zero value must be empty")
	}
}
