package broker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/personalconfig"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
)

// All content/path validation happens before the first sandbox write. Existing
// files and symlinks are preserved; per-file outcomes are returned to shell_run.
func (b *Broker) personalConfig(ctx context.Context, lease *Lease, taskID string) error {
	config, err := personalconfig.LoadTask(ctx, b.store, &commonv1.TenantContext{TenantId: lease.Owner}, taskID)
	if err != nil {
		return err
	}
	if config == nil {
		return nil
	}
	if _, err = personalconfig.Canonical(config.Document); err != nil {
		return err
	}
	if lease.Kind == "local" && len(config.Document.Files) > 0 {
		// Local development agents inherit the host HOME: never write there.
		sum := sha256.Sum256([]byte(lease.Owner + "\x00" + lease.ID()))
		lease.Env["HOME"] = "/tmp/agentruntime-personal-" + hex.EncodeToString(sum[:16])
	}
	result := applyPersonalFiles(ctx, lease, config)
	environment := map[string]any{}
	if lease.EnvironmentJSON != "" {
		_ = json.Unmarshal([]byte(lease.EnvironmentJSON), &environment)
	}
	environment["personal_config"] = result
	raw, _ := json.Marshal(environment)
	lease.EnvironmentJSON = string(raw)
	b.log.Info("personal config applied", "tenant", lease.Owner, "task", taskID, "version", config.Version, "digest", config.Digest)
	return nil
}

// Preflight does not create anything. It examines every target before writes.
const personalPreflight = `set -eu
case "${HOME:-}" in /*) ;; *) exit 20;; esac
[ ! -L "$HOME" ] || exit 21
for file do
  parent=$HOME
  oldIFS=$IFS; IFS=/; set -- $file; IFS=$oldIFS
  while [ "$#" -gt 1 ]; do
    parent=$parent/$1; shift
    [ ! -L "$parent" ] || exit 21
    if [ -e "$parent" ] && [ ! -d "$parent" ]; then exit 22; fi
  done
done
`

// Work relative to a pinned directory, verifying physical containment after
// each cd. noclobber atomically refuses existing files and final symlinks.
const personalWrite = `set -eu
umask 077
[ ! -L "$HOME" ] || exit 21
mkdir -p -- "$HOME"
cd -P -- "$HOME"
base=$PWD
file=$1
oldIFS=$IFS; IFS=/; set -- $file; IFS=$oldIFS
while [ "$#" -gt 1 ]; do
  part=$1; shift
  [ ! -L "$part" ] || exit 21
  mkdir -p -- "$part"
  [ ! -L "$part" ] || exit 21
  cd -P -- "$part"
  case "$PWD/" in "$base/"*) ;; *) exit 21;; esac
done
[ ! -e "$1" ] && [ ! -L "$1" ] || exit 10
set -C
cat > "$1"
`

func applyPersonalFiles(ctx context.Context, lease *Lease, config *pb.PersonalConfig) map[string]any {
	applied, skipped := []string{}, []string{}
	issues := []map[string]string{}
	result := map[string]any{"version": config.Version, "digest": config.Digest}
	files := config.Document.Files
	args := []string{"sh", "-c", personalPreflight, "personal-config"}
	for _, file := range files {
		args = append(args, file.Path)
	}
	options := sandbox.ExecOptions{Timeout: 30 * time.Second, Env: lease.Env}
	if len(files) > 0 {
		out, err := lease.Box.Exec(ctx, lease.EnvID, args, options)
		if err != nil || out.TimedOut || out.ExitCode == nil || *out.ExitCode != 0 {
			issues = append(issues, map[string]string{"path": "", "code": "personal_config_preflight_failed"})
			result["applied"], result["skipped"], result["errors"] = applied, skipped, issues
			return result
		}
	}
	for _, file := range files {
		options.Stdin = []byte(file.Content)
		out, err := lease.Box.Exec(ctx, lease.EnvID, []string{"sh", "-c", personalWrite, "personal-config", file.Path}, options)
		switch {
		case err == nil && !out.TimedOut && out.ExitCode != nil && *out.ExitCode == 0:
			applied = append(applied, file.Path)
		case err == nil && out.ExitCode != nil && *out.ExitCode == 10:
			skipped = append(skipped, file.Path)
		default:
			issues = append(issues, map[string]string{"path": file.Path, "code": "personal_config_write_failed"})
		}
	}
	result["applied"], result["skipped"], result["errors"] = applied, skipped, issues
	return result
}

// Reuse must not carry HOME files from a different pinned package into a task.
func (b *Broker) personalFilesCompatible(ctx context.Context, owner, before, after string) (bool, error) {
	if before == after {
		return true, nil
	}
	identity := func(task string) (string, error) {
		config, err := personalconfig.LoadTask(ctx, b.store, &commonv1.TenantContext{TenantId: owner}, task)
		if err != nil {
			return "", err
		}
		if config == nil || len(config.Document.Files) == 0 {
			return "", nil
		}
		return config.Digest, nil
	}
	old, err := identity(before)
	if err != nil {
		return false, err
	}
	next, err := identity(after)
	return old == next, err
}
