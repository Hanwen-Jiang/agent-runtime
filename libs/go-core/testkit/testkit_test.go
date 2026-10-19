package testkit_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

func TestFakeClockSleepWakesOnAdvance(t *testing.T) {
	clock := testkit.NewFakeClock()
	start := clock.Now()
	done := make(chan error, 1)
	go func() { done <- clock.Sleep(context.Background(), 10*time.Second) }()
	time.Sleep(10 * time.Millisecond)
	clock.Advance(9 * time.Second)
	select {
	case <-done:
		t.Fatal("woke up too early")
	case <-time.After(20 * time.Millisecond):
	}
	clock.Advance(time.Second)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("did not wake up")
	}
	if got := clock.Now().Sub(start); got != 10*time.Second {
		t.Fatalf("elapsed = %v", got)
	}
}

func TestFaultsFailNextOnly(t *testing.T) {
	faults := testkit.NewFaults()
	faults.FailNext("Get", providererr.Transient)
	if err := faults.Check("Get"); providererr.CategoryOf(err) != providererr.Transient {
		t.Fatalf("first Check = %v", err)
	}
	if err := faults.Check("Get"); err != nil {
		t.Fatalf("second Check = %v", err)
	}
	var none *testkit.Faults
	if err := none.Check("Get"); err != nil {
		t.Fatal("nil Faults must never fail")
	}
}
