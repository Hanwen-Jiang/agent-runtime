package ids

// S-03 验收：Task Service 生成的 ID 合乎契约（proto IdKind 的前缀、schemas/defs.schema.json 的格式），
// task_id 同时是 Task 子工作流的 ID（计划 S-03 难点“子工作流 ID”）。

import (
	"regexp"
	"slices"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

var (
	sessionPattern = regexp.MustCompile(`^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
	taskPattern    = regexp.MustCompile(`^tsk_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
)

func TestIDsMatchContractFormat(t *testing.T) {
	g := New(testkit.NewFakeClock(), nil)
	for range 1000 {
		if s := g.SessionID(); !sessionPattern.MatchString(s) || !ValidSessionID(s) {
			t.Fatalf("session_id 格式不对：%q", s)
		}
		if s := g.TaskID(); !taskPattern.MatchString(s) || !ValidTaskID(s) {
			t.Fatalf("task_id 格式不对：%q", s)
		}
	}
}

func TestIDsAreUniqueAndSortByCreation(t *testing.T) {
	clk := testkit.NewFakeClock()
	g := New(clk, nil)
	var got []string
	seen := map[string]bool{}
	for i := range 10000 {
		if i%1000 == 0 {
			clk.Advance(time.Millisecond)
		}
		id := g.TaskID()
		if seen[id] {
			t.Fatalf("task_id 重复：%s", id)
		}
		seen[id] = true
		got = append(got, id)
	}
	if !slices.IsSorted(got) {
		t.Error("同一毫秒内与跨毫秒生成的 task_id 都应按生成顺序排序")
	}
}

func TestValidatorsRejectMalformedIDs(t *testing.T) {
	for _, s := range []string{
		"", "tsk_", "ses_01J0000000000000000000000S", // 前缀不对
		"tsk_81J00000000000000000000000",            // 首字符超出 48 位时间戳范围
		"tsk_01J000000000000000000000OI",            // 含 Crockford base32 不用的字符
		"tsk_01J00000000000000000000000\n",          // 结尾换行
	} {
		if ValidTaskID(s) {
			t.Errorf("ValidTaskID(%q) 应为 false", s)
		}
	}
	if ValidSessionID("tsk_01J0000000000000000000000S") {
		t.Error("task_id 不是 session_id")
	}
}
