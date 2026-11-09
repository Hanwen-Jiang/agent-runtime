package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/files"
)

// fileGC runs a bounded operator-maintenance batch against the same providers
// as serve. No listener, Temporal worker or background daemon is started.
func fileGC(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	flags := flag.NewFlagSet("file-gc", flag.ContinueOnError)
	flags.SetOutput(stderr)
	tenantID := flags.String("tenant", "", "required tenant scope")
	workspace := flags.String("workspace", "", "required workspace scope")
	limit := flags.Int("limit", 100, "maximum objects per bounded batch (1–500)")
	prefix := flags.String("prefix", "uploads/", "uploads/ or file-derived/")
	cursor := flags.String("cursor", "", "ObjectStore page cursor from previous batch")
	if e := flags.Parse(args); e != nil {
		return 2
	}
	if flags.NArg() != 0 || *workspace == "" || *limit < 1 || *limit > 500 {
		return 2
	}
	scope := &commonv1.TenantContext{TenantId: *tenantID, WorkspaceId: *workspace}
	if _, e := tenancy.TenantScope(scope); e != nil {
		fmt.Fprintln(stderr, "invalid tenant/workspace scope")
		return 2
	}
	locations, e := config.StoreFromEnv(getenv)
	if e != nil {
		fmt.Fprintln(stderr, e)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	db, e := newRelationalStore(ctx, locations)
	if e != nil {
		fmt.Fprintln(stderr, "file-gc database unavailable")
		return 1
	}
	defer db.Close()
	if e = db.Migrate(ctx, files.Migrations()); e != nil {
		fmt.Fprintln(stderr, "file-gc migration failed")
		return 1
	}
	var objects objectstore.Provider
	options, configured, e := s3objects.OptionsFromEnv(getenv)
	if e != nil {
		fmt.Fprintln(stderr, e)
		return 1
	}
	if configured {
		objects, e = s3objects.New(options)
	} else {
		if locations.Production {
			fmt.Fprintln(stderr, "production file-gc requires configured ObjectStore")
			return 1
		}
		directory := getenv(config.EnvObjectStoreDir)
		if directory == "" {
			directory = filepath.Join(locations.DataDir, "objects")
		}
		key := make([]byte, 32)
		if _, e = rand.Read(key); e != nil {
			fmt.Fprintln(stderr, "file-gc initialization failed")
			return 1
		}
		objects, e = diskobjects.New(diskobjects.Options{Root: directory, SigningKey: key})
	}
	if e != nil {
		fmt.Fprintln(stderr, "file-gc object store unavailable")
		return 1
	}
	defer objects.Close()
	store, e := objects.Bind(scope)
	if e != nil {
		fmt.Fprintln(stderr, "file-gc scope unavailable")
		return 1
	}
	report, e := files.New(db).CollectGarbage(ctx, scope, store, files.RetentionOptions{Now: time.Now().UTC(), Limit: *limit, Prefix: *prefix, Cursor: *cursor})
	if e != nil {
		fmt.Fprintln(stderr, "file-gc batch failed:", e)
		return 1
	}
	if e = json.NewEncoder(stdout).Encode(report); e != nil {
		return 1
	}
	if report.Failed > 0 {
		return 1
	}
	return 0
}
