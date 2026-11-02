package environmentapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	envv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1/environmentv1connect"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

const testSession = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"

var testNow = time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
var ctx = context.Background()

// Minimal sandbox_envs fixture; production migration remains owned by Python SandboxLedger.
var testMigration = []relational.Migration{{Version: 201, Name: "environment_api_fixture", Statements: []string{`CREATE TABLE sandbox_envs (
 env_ref TEXT PRIMARY KEY, backend TEXT NOT NULL, kind TEXT NOT NULL, isolation TEXT NOT NULL, backend_env_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, writable BIGINT NOT NULL,
 runtime TEXT NOT NULL, image TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, ready_at TEXT NOT NULL,
 last_used_at TEXT NOT NULL, expires_at TEXT NOT NULL, max_until TEXT NOT NULL, destroyed_at TEXT NOT NULL,
 health_confirmed_at TEXT NOT NULL, reused_at TEXT NOT NULL, reason TEXT NOT NULL)`}}}

type harness struct {
	store  relational.Provider
	ing    *localingress.Provider
	client environmentv1connect.EnvironmentServiceClient
}

// All API transactions must be read-only; test seeding bypasses this wrapper.
type readOnlyProvider struct {
	relational.Provider
	t *testing.T
}
type readOnlyStore struct {
	relational.Store
	t *testing.T
}

func (p readOnlyProvider) Bind(tc *commonv1.TenantContext) (relational.Store, error) {
	s, err := p.Provider.Bind(tc)
	return readOnlyStore{s, p.t}, err
}
func (p readOnlyProvider) BindPlatform(string) (relational.Store, error) {
	p.t.Fatal("API must not read the platform directory")
	return nil, nil
}
func (p readOnlyProvider) Migrate(context.Context, []relational.Migration) error {
	p.t.Fatal("API must not migrate")
	return nil
}
func (s readOnlyStore) Transaction(c context.Context, o relational.TxOptions, fn func(context.Context, relational.Tx) error) error {
	if !o.ReadOnly {
		s.t.Fatal("API opened a writable transaction")
	}
	return s.Store.Transaction(c, o, fn)
}

func newHarness(t *testing.T, store relational.Provider) *harness {
	t.Helper()
	ing := localingress.New(localingress.Options{SigningKey: []byte(strings.Repeat("k", 32)), Limit: 1000, Window: time.Minute})
	srv, err := New(Deps{Ingress: ing, Store: readOnlyProvider{store, t}, Clock: func() time.Time { return testNow }})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Mount(mux)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return &harness{store: store, ing: ing, client: environmentv1connect.NewEnvironmentServiceClient(ts.Client(), ts.URL)}
}

func eachStore(t *testing.T, fn func(*testing.T, relational.Provider)) {
	t.Helper()
	for _, backend := range []string{"sqlite", "postgres"} {
		t.Run(backend, func(t *testing.T) {
			var store relational.Provider
			var err error
			if backend == "sqlite" {
				store, err = sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
			} else {
				cluster := pgtest.Current(t)
				store, err = postgres.New(ctx, postgres.Options{DSN: cluster.DSN(pgtest.AppRole, cluster.NewDatabase(t))})
			}
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = store.Close() })
			fn(t, store)
		})
	}
}

func request[T any](h *harness, tenant string, message *T) *connect.Request[T] {
	r := connect.NewRequest(message)
	if tenant != "" {
		r.Header().Set("Authorization", "Bearer "+h.ing.IssueToken(tenant, "u1", time.Hour))
	}
	return r
}

func seed(t *testing.T, store relational.Provider, tenant, ref string, values map[string]any) {
	t.Helper()
	if err := store.Migrate(ctx, testMigration); err != nil {
		t.Fatal(err)
	}
	row := map[string]any{
		"env_ref": ref, "backend": "kata", "kind": "k8s", "isolation": "microvm", "backend_env_id": "pod-secret-free",
		"workspace_id": "w1", "session_id": testSession, "task_id": "task-a", "writable": int64(1), "runtime": "python", "image": "image@sha256:abc",
		"state": "ready", "created_at": "2026-11-03T10:00:00.000000Z", "ready_at": "2026-11-03T10:01:00.000000Z",
		"last_used_at": "2026-11-03T11:55:00.000000Z", "expires_at": "2026-11-03T12:15:00.000000Z", "max_until": "2026-11-03T16:00:00.000000Z",
		"destroyed_at": "", "health_confirmed_at": "2026-11-03T11:55:00.000000Z", "reused_at": "", "reason": "PRIVATE TOKEN MUST NOT APPEAR",
	}
	for k, v := range values {
		row[k] = v
	}
	columns := []string{"env_ref", "backend", "kind", "isolation", "backend_env_id", "workspace_id", "session_id", "task_id", "writable", "runtime", "image", "state", "created_at", "ready_at", "last_used_at", "expires_at", "max_until", "destroyed_at", "health_confirmed_at", "reused_at", "reason"}
	var args []any
	var marks []string
	for i, k := range columns {
		args = append(args, row[k])
		marks = append(marks, "$"+strconv.Itoa(i+1))
	}
	db, err := store.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		t.Fatal(err)
	}
	err = db.Transaction(ctx, relational.TxOptions{}, func(c context.Context, tx relational.Tx) error {
		_, err := tx.Execute(c, "INSERT INTO sandbox_envs ("+strings.Join(columns, ",")+") VALUES ("+strings.Join(marks, ",")+")", args...)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestGetUsesOnlyAuthoritativeTenantLedger(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		seed(t, store, "t1", "sbe-a", map[string]any{"reused_at": "2026-11-03T11:55:00.000000Z"})
		r, err := h.client.GetEnvironment(ctx, request(h, "t1", &envv1.GetEnvironmentRequest{EnvRef: "sbe-a"}))
		if err != nil {
			t.Fatal(err)
		}
		e := r.Msg.Environment
		if !e.Ready || !e.Reused || e.TenantId != "t1" || e.WorkspaceId != "w1" || e.SessionId != testSession || e.BackendEnvId != "pod-secret-free" {
			t.Fatalf("wrong projection: %v", e)
		}
		if e.ExpiresAt.AsTime() != testNow.Add(15*time.Minute) || e.MaxUntil.AsTime() != testNow.Add(4*time.Hour) {
			t.Fatalf("expiry was inferred: %v", e)
		}
		if strings.Contains(protojson.Format(e), "PRIVATE") {
			t.Fatal("internal reason leaked")
		}
		for _, tc := range []struct {
			auth, requestTenant string
			code                connect.Code
		}{{"t2", "", connect.CodeNotFound}, {"t2", "t1", connect.CodePermissionDenied}, {"", "", connect.CodePermissionDenied}} {
			_, err := h.client.GetEnvironment(ctx, request(h, tc.auth, &envv1.GetEnvironmentRequest{TenantId: tc.requestTenant, EnvRef: "sbe-a"}))
			if connect.CodeOf(err) != tc.code {
				t.Fatalf("auth %q/%q: %v", tc.auth, tc.requestTenant, err)
			}
		}
	})
}

func TestReadyAndReuseCannotBeInferredFromStateActivityOrPod(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		cases := []map[string]any{
			{"health_confirmed_at": ""}, {"expires_at": ""}, {"expires_at": "2026-11-03T12:00:00.000000Z"},
			{"max_until": "2026-11-03T11:00:00.000000Z"}, {"state": "lost"}, {"state": "future-state"},
		}
		for i, v := range cases {
			ref := "sbe-case" + strconv.Itoa(i)
			seed(t, store, "t1", ref, v)
			r, err := h.client.GetEnvironment(ctx, request(h, "t1", &envv1.GetEnvironmentRequest{EnvRef: ref}))
			if err != nil {
				t.Fatal(err)
			}
			if r.Msg.Environment.Ready || r.Msg.Environment.Reused {
				t.Fatalf("inferred state: %v", r.Msg.Environment)
			}
		}
	})
}

func TestListFiltersBothWorkspaceAndSessionAndBoundsResults(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		seed(t, store, "t1", "sbe-a", nil)
		seed(t, store, "t1", "sbe-b", nil)
		seed(t, store, "t1", "sbe-other-workspace", map[string]any{"workspace_id": "w2"})
		seed(t, store, "t1", "sbe-other-session", map[string]any{"session_id": "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"})
		r, err := h.client.ListEnvironments(ctx, request(h, "t1", &envv1.ListEnvironmentsRequest{WorkspaceId: "w1", SessionId: testSession, Limit: 1}))
		if err != nil {
			t.Fatal(err)
		}
		if len(r.Msg.Environments) != 1 || r.Msg.Environments[0].EnvRef != "sbe-a" || !r.Msg.Truncated {
			t.Fatalf("wrong list: %v", r.Msg)
		}
		for _, m := range []*envv1.ListEnvironmentsRequest{{}, {WorkspaceId: "w1"}, {WorkspaceId: "w1", SessionId: testSession, Limit: 101}} {
			_, err = h.client.ListEnvironments(ctx, request(h, "t1", m))
			if connect.CodeOf(err) != connect.CodeInvalidArgument {
				t.Fatalf("invalid list accepted: %v", err)
			}
		}
	})
}

func TestMissingLedgerIsUnavailableNotEmptyOrReady(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		_, err := h.client.GetEnvironment(ctx, request(h, "t1", &envv1.GetEnvironmentRequest{EnvRef: "sbe-a"}))
		if connect.CodeOf(err) != connect.CodeUnavailable {
			t.Fatalf("missing ledger: %v", err)
		}
		_, err = h.client.ListEnvironments(ctx, request(h, "t1", &envv1.ListEnvironmentsRequest{WorkspaceId: "w1", SessionId: testSession}))
		if connect.CodeOf(err) != connect.CodeUnavailable {
			t.Fatalf("missing ledger list: %v", err)
		}
	})
}
