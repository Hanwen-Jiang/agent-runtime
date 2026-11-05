package envbuild

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// ImageRecord 是 sandbox_images 的一行（迁移 204，租户作用域）。
type ImageRecord struct {
	ImageKey, Owner, Kind, Path, Dockerfile, State, CreatedAt string
	WorkspaceID, Context, BaseImage, Image, Digest            string
	SizeBytes                                                 int64
	Reason, ReadyAt, LastUsedAt                               string
	BuildMS                                                   int64
	DeletedAt                                                 string
}

var imageColumns = []string{"image_key", "kind", "path", "dockerfile", "state", "created_at", "workspace_id", "context", "base_image", "image",
	"digest", "size_bytes", "reason", "ready_at", "last_used_at", "build_ms", "deleted_at"}

var imageColumnList = strings.Join(imageColumns, ", ")

func (r ImageRecord) values() []any {
	return []any{r.ImageKey, r.Kind, r.Path, r.Dockerfile, r.State, r.CreatedAt, r.WorkspaceID, r.Context, r.BaseImage, r.Image, r.Digest,
		r.SizeBytes, r.Reason, r.ReadyAt, r.LastUsedAt, r.BuildMS, r.DeletedAt}
}

func imageFromRow(owner string, row relational.Row) ImageRecord {
	s := func(k string) string { return ledger.Str(row, k) }
	return ImageRecord{
		ImageKey: s("image_key"), Owner: owner, Kind: s("kind"), Path: s("path"), Dockerfile: s("dockerfile"), State: s("state"),
		CreatedAt: s("created_at"), WorkspaceID: s("workspace_id"), Context: s("context"), BaseImage: s("base_image"), Image: s("image"),
		Digest: s("digest"), SizeBytes: ledger.Int(row, "size_bytes"), Reason: s("reason"), ReadyAt: s("ready_at"), LastUsedAt: s("last_used_at"),
		BuildMS: ledger.Int(row, "build_ms"), DeletedAt: s("deleted_at"),
	}
}

// Store 是 sandbox_images 的读写（表在环境表的迁移 204 里）。
type Store struct {
	ledger *ledger.Ledger
}

// NewStore 构造。
func NewStore(l *ledger.Ledger) *Store { return &Store{ledger: l} }

// Get 取一行；没有时 nil。
func (s *Store) Get(ctx context.Context, owner, key string) (*ImageRecord, error) {
	var out *ImageRecord
	err := s.ledger.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT "+imageColumnList+" FROM sandbox_images WHERE image_key = $1", key)
		if err != nil || !ok {
			return err
		}
		r := imageFromRow(owner, row)
		out = &r
		return nil
	})
	return out, err
}

// All 是这个租户的全部记录（按建的时间）。
func (s *Store) All(ctx context.Context, owner string) ([]ImageRecord, error) {
	var out []ImageRecord
	err := s.ledger.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT "+imageColumnList+" FROM sandbox_images ORDER BY created_at")
		for _, row := range rows {
			out = append(out, imageFromRow(owner, row))
		}
		return err
	})
	return out, err
}

// BeginBuild 记一行 building；同一个事务里核对个数配额（building 与 ready 都算；maxImages 为 0 不限），超了 denied。已有这一行时
// 原样返回（created 为 false）；retryFailed 时失败的那一行重来。last_used_at 记下开始的时间，构建期间是心跳（见 Manager）。
func (s *Store) BeginBuild(ctx context.Context, owner string, d Definition, key, workspaceID, baseImage string, maxImages int, retryFailed bool) (ImageRecord, bool, error) {
	now := ledger.ISO(s.ledger.Now())
	rec := ImageRecord{ImageKey: key, Owner: owner, Kind: d.Kind, Path: d.Path, Dockerfile: d.Dockerfile, State: "building", CreatedAt: now,
		WorkspaceID: workspaceID, Context: d.Context, BaseImage: baseImage, LastUsedAt: now}
	created := false
	err := s.ledger.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, "INSERT INTO sandbox_quota_lock (id, version) VALUES ('images', 1) ON CONFLICT (id) DO UPDATE SET version = sandbox_quota_lock.version + 1"); err != nil {
			return err
		}
		row, exists, err := tx.QueryOne(ctx, "SELECT "+imageColumnList+" FROM sandbox_images WHERE image_key = $1", key)
		if err != nil {
			return err
		}
		if exists {
			state := ledger.Str(row, "state")
			if state != "deleted" && !(retryFailed && state == "failed") {
				rec = imageFromRow(owner, row)
				return nil
			}
		}
		count, _, err := tx.QueryOne(ctx, "SELECT COUNT(*) AS n FROM sandbox_images WHERE state IN ('building', 'ready')")
		if err != nil {
			return err
		}
		if maxImages > 0 && ledger.Int(count, "n") >= int64(maxImages) {
			return providererr.New(providererr.Denied, "environment_quota_exceeded", fmt.Sprintf("项目环境的镜像最多 %d 个，先清掉不用的", maxImages))
		}
		if exists {
			if _, err := tx.Execute(ctx, "DELETE FROM sandbox_images WHERE image_key = $1", key); err != nil {
				return err
			}
		}
		placeholders := make([]string, len(imageColumns))
		for i := range imageColumns {
			placeholders[i] = fmt.Sprintf("$%d", i+1)
		}
		if _, err := tx.Execute(ctx, "INSERT INTO sandbox_images ("+imageColumnList+") VALUES ("+strings.Join(placeholders, ", ")+")", rec.values()...); err != nil {
			return err
		}
		created = true
		return nil
	})
	return rec, created, err
}

func (s *Store) set(ctx context.Context, owner, key, where string, values map[string]any) (int64, error) {
	// 列的顺序固定（按 imageColumns），SQL 文本稳定。where 是另加的条件（不带参数）。
	var sets []string
	var args []any
	for _, c := range imageColumns {
		if v, ok := values[c]; ok {
			args = append(args, v)
			sets = append(sets, fmt.Sprintf("%s = $%d", c, len(args)))
		}
	}
	args = append(args, key)
	sql := "UPDATE sandbox_images SET " + strings.Join(sets, ", ") + fmt.Sprintf(" WHERE image_key = $%d", len(args))
	if where != "" {
		sql += " AND " + where
	}
	var n int64
	err := s.ledger.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		var err error
		n, err = tx.Execute(ctx, sql, args...)
		return err
	})
	return n, err
}

// MarkReady 记为就绪。
func (s *Store) MarkReady(ctx context.Context, owner, key, image, digest string, size, buildMS int64) error {
	now := ledger.ISO(s.ledger.Now())
	_, err := s.set(ctx, owner, key, "", map[string]any{"state": "ready", "image": image, "digest": digest, "size_bytes": size, "build_ms": buildMS,
		"ready_at": now, "last_used_at": now, "reason": ""})
	return err
}

// MarkFailed 记为失败。日志的结论在最后，留尾部（500 个字）；last_used_at 记下失败的时间，过了 FailedRetryAfter 可以再试。截过之后
// 再去一遍凭据的样子：截断可能把签名地址切成没有 https:// 与 ? 的半截（EV-TB）。
func (s *Store) MarkFailed(ctx context.Context, owner, key, reason string, buildMS int64) error {
	if r := []rune(reason); len(r) > 500 {
		reason = string(r[len(r)-500:])
	}
	_, err := s.set(ctx, owner, key, "", map[string]any{"state": "failed", "reason": Scrub(reason), "build_ms": buildMS,
		"last_used_at": ledger.ISO(s.ledger.Now())})
	return err
}

// Touch 记下用到的时间。
func (s *Store) Touch(ctx context.Context, owner, key string) error {
	_, err := s.set(ctx, owner, key, "", map[string]any{"last_used_at": ledger.ISO(s.ledger.Now())})
	return err
}

// MarkDeleted 记为已清掉。
func (s *Store) MarkDeleted(ctx context.Context, owner, key string) error {
	_, err := s.set(ctx, owner, key, "", map[string]any{"state": "deleted", "deleted_at": ledger.ISO(s.ledger.Now())})
	return err
}

// Heartbeat 在构建中的这一行记一次心跳；这一行已经不是 building（被别人接手后结束了）时返回 false。
func (s *Store) Heartbeat(ctx context.Context, owner, key string) (bool, error) {
	n, err := s.set(ctx, owner, key, "state = 'building'", map[string]any{"last_used_at": ledger.ISO(s.ledger.Now())})
	return n > 0, err
}

// Claim 接手心跳停了的构建：last_used_at 早于 staleBefore 的 building 行改成现在，只有一个调用方改得成（返回 true）。
func (s *Store) Claim(ctx context.Context, owner, key string, staleBefore time.Time) (bool, error) {
	cutoff := ledger.ISO(staleBefore)
	var n int64
	err := s.ledger.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		var err error
		n, err = tx.Execute(ctx, "UPDATE sandbox_images SET last_used_at = $1 WHERE image_key = $2 AND state = 'building' AND last_used_at < $3",
			ledger.ISO(s.ledger.Now()), key, cutoff)
		return err
	})
	return n > 0, err
}
