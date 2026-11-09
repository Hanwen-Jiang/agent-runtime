package taskapi

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"net/http"
	"os"
	"regexp"
	"time"
)

var fileIdentityPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// A dedicated internal service credential can assert an existing Task actor. It
// is never accepted on Task submission, account, workspace, or external routes.
func (s *Server) fileIdentity(r *http.Request) (ingress.Identity, error) {
	if supplied := r.Header.Get("X-Ar-File-Token"); supplied != "" {
		expected := os.Getenv("AR_FILE_INTERNAL_TOKEN")
		if expected == "" || subtle.ConstantTimeCompare([]byte(supplied), []byte(expected)) != 1 {
			return ingress.Identity{}, missing("file service identity unavailable")
		}
		tenant, user := r.Header.Get("X-Ar-File-Tenant"), r.Header.Get("X-Ar-File-User")
		if !fileIdentityPattern.MatchString(tenant) || !fileIdentityPattern.MatchString(user) {
			return ingress.Identity{}, invalid("invalid internal file identity")
		}
		return ingress.Identity{TenantID: tenant, UserID: user}, nil
	}
	return s.authenticate(r.Context(), r.Header, "task_upload")
}

// Memory's existing HMAC assertion is minted from the authenticated server
// identity, so public JWTs need not be accepted by the Python internal service.
func knowledgeAssertion(t *commonv1.TenantContext, fallback string) string {
	key := os.Getenv("AR_KNOWLEDGE_SIGNING_KEY")
	if key == "" {
		return fallback
	}
	raw, _ := json.Marshal(map[string]any{"t": t.GetTenantId(), "u": t.GetUserId(), "w": t.GetWorkspaceId(), "exp": time.Now().Add(5 * time.Minute).Unix()})
	payload := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(payload))
	return "Bearer " + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
