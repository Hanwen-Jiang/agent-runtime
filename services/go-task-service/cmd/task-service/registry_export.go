package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

// registryExportProfiles emits a catalog from the actual Registry database.
// It never seeds, promotes, or mutates profile contents. Redirect stdout to a
// temporary file and install it as the Gateway ConfigMap only after success.
func registryExportProfiles(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) != 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	ctx := context.Background()
	where, err := config.StoreFromEnv(getenv)
	if err != nil {
		fmt.Fprintln(stderr, "registry-export-profiles: invalid store configuration")
		return 2
	}
	store, err := newRelationalStore(ctx, where)
	if err != nil {
		fmt.Fprintln(stderr, "registry-export-profiles: cannot open Registry store")
		return 1
	}
	defer store.Close()
	reg, err := registry.New(registry.Options{Store: store, SchemaDir: where.SchemasDir})
	if err != nil {
		fmt.Fprintln(stderr, "registry-export-profiles: cannot initialize Registry")
		return 1
	}
	exported, err := reg.ExportModelProfiles(ctx)
	if err != nil || len(exported.Profiles) == 0 {
		fmt.Fprintln(stderr, "registry-export-profiles: cannot export published default profiles")
		return 1
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(exported); err != nil {
		fmt.Fprintln(stderr, "registry-export-profiles: cannot write catalog")
		return 1
	}
	return 0
}
