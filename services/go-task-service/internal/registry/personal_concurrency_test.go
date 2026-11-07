package registry_test

import (
	"context"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
)

type pausePersonalRead struct {
	relational.Provider
	enabled       atomic.Bool
	ready, resume chan struct{}
}

func (p *pausePersonalRead) Bind(t *commonv1.TenantContext) (relational.Store, error) {
	s, e := p.Provider.Bind(t)
	if e != nil {
		return nil, e
	}
	return &pausePersonalStore{Store: s, p: p}, nil
}

type pausePersonalStore struct {
	relational.Store
	p *pausePersonalRead
}

func (s *pausePersonalStore) Transaction(ctx context.Context, o relational.TxOptions, f func(context.Context, relational.Tx) error) error {
	return s.Store.Transaction(ctx, o, func(ctx context.Context, tx relational.Tx) error { return f(ctx, &pausePersonalTx{Tx: tx, p: s.p}) })
}

type pausePersonalTx struct {
	relational.Tx
	p *pausePersonalRead
}

func (t *pausePersonalTx) QueryOne(ctx context.Context, q string, a ...any) (relational.Row, bool, error) {
	row, found, err := t.Tx.QueryOne(ctx, q, a...)
	if err == nil && strings.HasPrefix(q, "SELECT kind, name, version, state, task_family, document") && strings.Contains(q, " FROM registry_entries WHERE kind = $1 AND name = $2") && t.p.enabled.CompareAndSwap(true, false) {
		close(t.p.ready)
		select {
		case <-t.p.resume:
		case <-ctx.Done():
			return nil, false, ctx.Err()
		}
	}
	return row, found, err
}
func TestPersonalConfigPublicationDoesNotUseTwoDatabaseSnapshots(t *testing.T) {
	for _, optimistic := range []bool{false, true} {
		name := "identical-create"
		if optimistic {
			name = "expected-version-change"
		}
		t.Run(name, func(t *testing.T) {
			var b backend
			for _, candidate := range backends {
				if candidate.name == "postgres" {
					b = candidate
				}
			}
			p := b.open(t)
			gate := &pausePersonalRead{Provider: p, ready: make(chan struct{}), resume: make(chan struct{})}
			h := newHarness(t, gate)
			other := newHarness(t, p)
			first, second, expected := "same", "same", ""
			if optimistic {
				if _, _, e := other.reg.PutPersonalConfig(context.Background(), alice, &pb.PersonalConfigDocument{Instructions: "base"}, ""); e != nil {
					t.Fatal(e)
				}
				first, second, expected = "first", "other", "user/v1"
			}
			type result struct {
				config  *pb.PersonalConfig
				created bool
				err     error
			}
			done := make(chan result, 1)
			gate.enabled.Store(true)
			go func() {
				c, created, e := h.reg.PutPersonalConfig(context.Background(), alice, &pb.PersonalConfigDocument{Instructions: first}, expected)
				done <- result{c, created, e}
			}()
			select {
			case <-gate.ready:
			case <-time.After(10 * time.Second):
				close(gate.resume)
				t.Fatal("first publisher did not read")
			}
			concurrent, _, e := other.reg.PutPersonalConfig(context.Background(), alice, &pb.PersonalConfigDocument{Instructions: second}, expected)
			close(gate.resume)
			if e != nil {
				t.Fatal(e)
			}
			var got result
			select {
			case got = <-done:
			case <-time.After(10 * time.Second):
				t.Fatal("first publisher did not finish")
			}
			if optimistic {
				if providererr.CategoryOf(got.err) != providererr.Conflict {
					t.Fatalf("stale expected version accepted: config=%v error=%v", got.config, got.err)
				}
			} else {
				if got.err != nil || got.created || got.config.GetVersion() != concurrent.GetVersion() {
					t.Fatalf("identical concurrent publication must reuse version: config=%v created=%v error=%v", got.config, got.created, got.err)
				}
			}
			active, e := other.reg.GetPersonalConfig(context.Background(), alice, "")
			if e != nil {
				t.Fatal(e)
			}
			if active.GetVersion() != concurrent.GetVersion() || active.GetDocument().GetInstructions() != second {
				t.Fatalf("concurrent version changed: %v", active)
			}
		})
	}
}
