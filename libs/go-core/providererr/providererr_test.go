package providererr_test

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestOnlyTransientIsRetryable(t *testing.T) {
	for number := range commonv1.ErrorCategory_name {
		category := commonv1.ErrorCategory(number)
		if number == 0 {
			continue
		}
		err := providererr.New(category, "x", "")
		if got, want := err.Retryable(), category == providererr.Transient; got != want {
			t.Errorf("%v: Retryable() = %v, want %v", category, got, want)
		}
		if got := providererr.IsRetryable(fmt.Errorf("wrapped: %w", err)); got != (category == providererr.Transient) {
			t.Errorf("%v: IsRetryable through wrapping = %v", category, got)
		}
	}
}

func TestCategoryOfNonProviderError(t *testing.T) {
	if got := providererr.CategoryOf(errors.New("plain")); got != commonv1.ErrorCategory_ERROR_CATEGORY_UNSPECIFIED {
		t.Fatalf("CategoryOf(plain) = %v", got)
	}
	if providererr.IsRetryable(errors.New("plain")) {
		t.Fatal("plain errors must not be retryable")
	}
}

func TestCauseDoesNotLeakIntoText(t *testing.T) {
	cause := errors.New("password=hunter2")
	err := providererr.Wrap(providererr.Transient, "unavailable", "数据库暂时不可用", cause)
	if strings.Contains(err.Error(), "hunter2") {
		t.Fatalf("Error() leaked the cause: %q", err.Error())
	}
	if !errors.Is(err, cause) {
		t.Fatal("cause must stay reachable through Unwrap")
	}
}

func TestProtoRoundTrip(t *testing.T) {
	err := providererr.New(providererr.Conflict, "unique_violation", "name 已存在")
	back := providererr.FromProto(err.Proto())
	if back.Category != providererr.Conflict || back.Code != "unique_violation" || back.Message != "name 已存在" {
		t.Fatalf("round trip = %+v", back)
	}
}
