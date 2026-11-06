package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

type personalShell struct{ sandbox.Sandbox }

func (personalShell) Exec(ctx context.Context, _ string, argv []string, o sandbox.ExecOptions) (sandbox.ExecResult, error) {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = strings.NewReader(string(o.Stdin))
	cmd.Env = os.Environ()
	for k, v := range o.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	out, err := cmd.CombinedOutput()
	code := 0
	if err != nil {
		if e, ok := err.(*exec.ExitError); ok {
			code = e.ExitCode()
		} else {
			return sandbox.ExecResult{}, err
		}
	}
	return sandbox.ExecResult{ExitCode: &code, Stdout: out}, nil
}
func TestPersonalFilesPreflightThenNoClobber(t *testing.T) {
	home := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(home, "linked")); err != nil {
		t.Fatal(err)
	}
	lease := &Lease{Box: personalShell{}, Env: map[string]string{"HOME": home}}
	config := &pb.PersonalConfig{Version: "user/v1", Digest: "digest", Document: &pb.PersonalConfigDocument{Files: []*pb.PersonalConfigFile{{Path: "first.txt", Content: "first"}, {Path: "linked/no.txt", Content: "bad"}}}}
	got, deliveryErr := applyPersonalFiles(context.Background(), lease, config)
	if deliveryErr == nil {
		t.Fatal("invalid HOME must fail delivery")
	}
	if len(got["errors"].([]map[string]string)) != 1 {
		t.Fatalf("preflight: %v", got)
	}
	if _, err := os.Stat(filepath.Join(home, "first.txt")); !os.IsNotExist(err) {
		t.Fatal("partial write before validation")
	}
	config.Document.Files = []*pb.PersonalConfigFile{{Path: ".config/tool/config.toml", Content: "theme='dark'"}, {Path: "existing.txt", Content: "replace"}, {Path: "symlink.txt", Content: "replace"}}
	os.WriteFile(filepath.Join(home, "existing.txt"), []byte("original"), 0600)
	os.WriteFile(filepath.Join(outside, "target"), []byte("outside"), 0600)
	os.Symlink(filepath.Join(outside, "target"), filepath.Join(home, "symlink.txt"))
	got, deliveryErr = applyPersonalFiles(context.Background(), lease, config)
	if deliveryErr != nil {
		t.Fatal(deliveryErr)
	}
	if len(got["applied"].([]string)) != 1 || len(got["skipped"].([]string)) != 2 || len(got["errors"].([]map[string]string)) != 0 {
		t.Fatalf("results: %v", got)
	}
	for path, want := range map[string]string{filepath.Join(home, "existing.txt"): "original", filepath.Join(outside, "target"): "outside", filepath.Join(home, ".config/tool/config.toml"): "theme='dark'"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("%s: %s %v", path, data, err)
		}
	}
	st, _ := os.Stat(filepath.Join(home, ".config/tool/config.toml"))
	if st.Mode().Perm() != 0600 {
		t.Fatalf("mode %v", st.Mode())
	}
}
