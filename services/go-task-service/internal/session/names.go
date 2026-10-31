package session

// Session 工作流与 Task 子工作流靠这些字符串对上（附录 K.1、K.5）。取值必须等于
// proto/agentruntime/workflow/v1/names.proto 里的 wire_name，由 names_test.go 比对。
const (
	WorkflowName     = "SessionWorkflow"
	TaskWorkflowName = "TaskWorkflow"

	// 发给 Session 工作流，载荷为 SubmitTaskSignal。
	SignalSubmitTask = "submit_task"
	// 发给 Session 工作流，载荷为 CancelTaskSignal。
	SignalCancelTask = "cancel_task"
	// 发给 Session 工作流，载荷为 EditQueuedTaskSignal。
	SignalEditQueuedTask = "edit_queued_task"
	// Task 子工作流结束时发给 Session 工作流，载荷为 TaskWorkflowResult。
	// Continue-As-New 之后，新的运行拿不到上一次运行启动的子工作流的 future，只能靠这个信号得知结束。
	SignalTaskFinished = "task_finished"

	QuerySessionState = "session_state"

	// Task 工作流的查询与信号，Task API 直接发给 Task 子工作流。
	QueryTaskState     = "task_state"
	SignalHumanReply   = "human_reply"
	SignalTaskGuidance = "task_guidance"

	// Session 工作流与它的 activity 所在的任务队列。
	SessionTaskQueue = "session"
	// Task 子工作流所在的任务队列，由 Python Worker 消费。
	TaskTaskQueue = "task"
)

// Session 工作流的 activity 名。
const (
	AcceptTaskActivity = "AcceptTask"
	FinishTaskActivity = "FinishTask"
	// RecordQueuedEditActivity 记下修改排队中的消息的结果（docs/plan/running-task-interaction.md 第 2 节）。
	RecordQueuedEditActivity = "RecordQueuedEdit"
	// PublishTaskEventActivity 是跨语言的 activity：Python 的 Task 工作流把进度事件交给 Session 的 Worker（T-11b），
	// 名字取自 names.proto 的 ActivityName。
	PublishTaskEventActivity = "publish_task_event"
)
