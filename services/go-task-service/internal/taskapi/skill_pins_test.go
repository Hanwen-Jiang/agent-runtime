package taskapi

// N-14：提交时固定的 Skill（PinnedVersions.skills）另记在 Task 的记录上（tasks.skill_pins，JSON），Worker 的 Planner 目录与
// skill_read 按 task_id 读它，不经工作流。没有 Skill 时这一列为空。

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func (f *apiFixture) skillPins(tenant, taskID string) string {
	f.t.Helper()
	db, err := f.store.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		f.t.Fatal(err)
	}
	var pins string
	if err := db.Transaction(context.Background(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT skill_pins FROM tasks WHERE task_id = $1`, taskID)
		if err == nil && found {
			pins, _ = row["skill_pins"].(string)
		}
		return err
	}); err != nil {
		f.t.Fatal(err)
	}
	return pins
}

func TestSubmittedTasksRecordTheirPinnedSkills(t *testing.T) {
	f := newAPI(t)
	f.pins.v.Skills = map[string]string{"weekly_report": "platform/v2", "my_style": "user/v1"}
	resp, err := f.submit(f.token("t1", "u1"), newRequest("with-skills"))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if err := json.Unmarshal([]byte(f.skillPins("t1", resp.GetTaskId())), &got); err != nil || got["weekly_report"] != "platform/v2" || got["my_style"] != "user/v1" || len(got) != 2 {
		t.Fatalf("Task 的记录上有固定的 Skill：%v %v", got, err)
	}
	if _, sig := f.lastSubmit(); sig.GetTask().GetPinnedVersions().GetSkills()["weekly_report"] != "platform/v2" {
		t.Errorf("工作流输入里照样带着：%v", sig.GetTask().GetPinnedVersions())
	}

	f.pins.v.Skills = nil
	plain, err := f.submit(f.token("t1", "u1"), newRequest("no-skills"))
	if err != nil {
		t.Fatal(err)
	}
	if pins := f.skillPins("t1", plain.GetTaskId()); pins != "" {
		t.Errorf("没有 Skill 时为空：%q", pins)
	}
}
