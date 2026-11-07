package cfsandbox_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"net/http"
	"os"
	"testing"
	"time"
)

func TestLiveCloudflareScopedObjectTransfer(t *testing.T) {
	if os.Getenv("AR_LIVE_CF_TRANSFER") != "1" {
		t.Skip("explicit isolated Cloudflare/R2 acceptance required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	p := provider(t, os.Getenv("AR_CLOUDFLARE_BRIDGE_URL"), os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN"), "universal", "python")
	box := conformance.Must(p.Bind(conformance.TenantA()))
	env := conformance.Must(box.Create(ctx, sandbox.Spec{Runtime: "python", Network: "none", IdleTimeout: 3 * time.Minute}))
	t.Cleanup(func() {
		c, end := context.WithTimeout(context.Background(), 30*time.Second)
		defer end()
		if e := box.Destroy(c, env.ID); e != nil {
			t.Error("isolated CF environment cleanup failed")
		}
	})
	identity, identityErr := box.Exec(ctx, env.ID, []string{"sha256sum", "/usr/local/bin/sandbox-agent"}, sandbox.ExecOptions{Timeout: 5 * time.Second})
	t.Logf("live agent identity=%q error=%v", identity.Stdout, identityErr)
	if os.Getenv("AR_EXPECTED_CF_AGENT_SHA") == "" || string(identity.Stdout) != os.Getenv("AR_EXPECTED_CF_AGENT_SHA")+"  /usr/local/bin/sandbox-agent\n" {
		t.Fatal("Cloudflare container rollout has not loaded the frozen agent")
	}
	network := box.(sandbox.ScopedTransferNetwork)
	transfer := box.(sandbox.Transfers)
	raw := make([]byte, 16)
	if _, e := rand.Read(raw); e != nil {
		t.Fatal(e)
	}
	store, e := s3objects.New(s3objects.Options{Endpoint: os.Getenv("AR_S3_ENDPOINT"), Bucket: os.Getenv("AR_S3_BUCKET"), AccessKeyID: os.Getenv("AR_S3_ACCESS_KEY_ID"), SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")), Region: os.Getenv("AR_S3_REGION"), Prefix: "cf-transfer-accept/" + hex.EncodeToString(raw) + "/"})
	if e != nil {
		t.Fatal("R2 configuration")
	}
	objects := conformance.Must(store.Bind(conformance.TenantA()))
	data := bytes.Repeat([]byte("immutable CF object\n"), 4096)
	if e = box.WriteFile(ctx, env.ID, "data.bin", data); e != nil {
		t.Fatal("write fixture", e)
	}
	prepared := conformance.Must(transfer.PrepareTransfer(ctx, env.ID, "data.bin"))
	signed := conformance.Must(objects.SignedUpload(ctx, "one", time.Minute, prepared.SHA256, prepared.SizeBytes))
	t.Cleanup(func() {
		c, end := context.WithTimeout(context.Background(), 30*time.Second)
		defer end()
		if _, e := objects.Delete(c, "one"); e != nil {
			t.Error("R2 cleanup failed")
		}
	})
	nextID := func() string {
		b := make([]byte, 16)
		if _, e := rand.Read(b); e != nil {
			t.Fatal(e)
		}
		return "sbg-" + hex.EncodeToString(b)
	}
	cap := nextID()
	target := conformance.Must(network.GrantTransferTarget(ctx, env.ID, cap, "accept-task", "PUT", prepared.SizeBytes, time.Now().Add(90*time.Second), sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers}))
	if len(target.Headers) != 0 || target.Proxy != "" {
		t.Fatal("signed credentials leaked into guest")
	}
	if e = box.WriteFile(ctx, env.ID, "data.bin", []byte("changed")); e != nil {
		t.Fatal(e)
	}
	receipt, e := transfer.CommitTransfer(ctx, env.ID, prepared, "one", target)
	if e != nil {
		_ = box.WriteFile(ctx, env.ID, "diagnose.bin", data)
		diagnostic, diagErr := box.Exec(ctx, env.ID, []string{"curl", "-sS", "--max-time", "15", "-X", "PUT", "--data-binary", "@/workspace/diagnose.bin", "-w", " HTTP:%{http_code}", target.URL}, sandbox.ExecOptions{Timeout: 20 * time.Second})
		t.Logf("gateway diagnostic status=%v stdout=%q stderr=%q", diagErr, diagnostic.Stdout, diagnostic.Stderr)
		t.Fatal("real scoped upload failed", e)
	}
	if receipt.SHA256 != prepared.SHA256 {
		t.Fatal("wrong upload digest")
	}
	used := conformance.Must(network.RevokeTransferTarget(ctx, env.ID, cap))
	if used != int64(len(data)) {
		t.Fatalf("upload accounting %d", used)
	}
	got := conformance.Must(objects.Get(ctx, "one"))
	if !bytes.Equal(got.Data, data) {
		t.Fatal("source mutation changed uploaded bytes")
	}
	// Revocation is checked per request even before scheduled expiry cleanup.
	probe := func(target sandbox.TransferTarget, method string) int {
		out, e := box.Exec(ctx, env.ID, []string{"curl", "-s", "--max-time", "10", "-X", method, "-o", "/dev/null", "-w", "%{http_code}", target.URL}, sandbox.ExecOptions{Timeout: 15 * time.Second})
		if e != nil {
			t.Fatal(e)
		}
		if string(out.Stdout) != "403" {
			t.Fatalf("revoked/wrong-method request: %s", out.Stdout)
		}
		return *out.ExitCode
	}
	probe(target, "GET")
	if e = network.AcknowledgeTransferTarget(ctx, env.ID, cap); e != nil {
		t.Fatal(e)
	}
	// Acknowledge removes accounting, but the tombstone forbids delayed remint.
	if _, e = network.GrantTransferTarget(ctx, env.ID, cap, "accept-task", "PUT", prepared.SizeBytes, time.Now().Add(time.Minute), sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers}); e == nil {
		t.Fatal("revoked capability resurrected")
	}
	download := conformance.Must(objects.TemporaryURL(ctx, "one", time.Minute, "GET"))
	getCap := nextID()
	getTarget := conformance.Must(network.GrantTransferTarget(ctx, env.ID, getCap, "accept-task", "GET", prepared.SizeBytes, time.Now().Add(time.Minute), sandbox.TransferTarget{URL: download}))
	other := conformance.Must(p.Bind(conformance.TenantB())).(sandbox.ScopedTransferNetwork)
	if _, e = other.RevokeTransferTarget(ctx, env.ID, getCap); e == nil {
		t.Fatal("cross-tenant capability access")
	}
	if _, e = box.Exec(ctx, env.ID, []string{"rm", "data.bin"}, sandbox.ExecOptions{}); e != nil {
		t.Fatal(e)
	}
	restore := box.(sandbox.BlockTransfers)
	restoreID := hex.EncodeToString(raw)
	if e = restore.RestoreBatch(ctx, env.ID, restoreID, 1<<20, []sandbox.RestoreBlock{{SHA256: prepared.SHA256, SizeBytes: prepared.SizeBytes, Target: getTarget}}); e != nil {
		t.Fatal("real scoped download failed", e)
	}
	if e = restore.RestoreFinish(ctx, env.ID, restoreID, []sandbox.FileEntry{{Path: "restored.bin", Type: "file", Size: prepared.SizeBytes, Mode: 0600, SHA256: prepared.SHA256}}); e != nil {
		t.Fatal("restore failed", e)
	}
	if got := conformance.Must(box.ReadFile(ctx, env.ID, "restored.bin")); !bytes.Equal(got, data) {
		t.Fatal("restore differs")
	}
	if used = conformance.Must(network.RevokeTransferTarget(ctx, env.ID, getCap)); used != int64(len(data)) {
		t.Fatalf("download accounting %d", used)
	}
	if e = network.AcknowledgeTransferTarget(ctx, env.ID, getCap); e != nil {
		t.Fatal(e)
	}
	expired := nextID()
	expiredTarget := conformance.Must(network.GrantTransferTarget(ctx, env.ID, expired, "accept-task", "GET", prepared.SizeBytes, time.Now().Add(3*time.Second), sandbox.TransferTarget{URL: download}))
	time.Sleep(4 * time.Second)
	probe(expiredTarget, "GET")
	if used = conformance.Must(network.RevokeTransferTarget(ctx, env.ID, expired)); used != 0 {
		t.Fatal("expired transfer charged bytes")
	}
	if e = network.AcknowledgeTransferTarget(ctx, env.ID, expired); e != nil {
		t.Fatal(e)
	}
	// Revoke a real streaming upload after the gateway has consumed bytes.
	slowCap := nextID()
	slowSigned := conformance.Must(objects.SignedUpload(ctx, "slow", time.Minute, prepared.SHA256, prepared.SizeBytes))
	t.Cleanup(func() {
		c, end := context.WithTimeout(context.Background(), 30*time.Second)
		defer end()
		if _, e := objects.Delete(c, "slow"); e != nil {
			t.Error("slow object cleanup failed")
		}
	})
	slowTarget := conformance.Must(network.GrantTransferTarget(ctx, env.ID, slowCap, "accept-task", "PUT", prepared.SizeBytes, time.Now().Add(time.Minute), sandbox.TransferTarget{URL: slowSigned.URL, Headers: slowSigned.Headers}))
	code := fmt.Sprintf("import http.client,time\nc=http.client.HTTPConnection('ar-transfer.internal',timeout=10)\nc.putrequest('PUT','/%s')\nc.putheader('Content-Length','%d')\nc.endheaders()\nf=open('/workspace/restored.bin','rb')\nwhile data:=f.read(4096):\n c.send(data);time.sleep(.1)\nprint(c.getresponse().status)\n", slowCap, prepared.SizeBytes)
	done := make(chan error, 1)
	go func() {
		_, e := box.Exec(ctx, env.ID, []string{"python", "-c", code}, sandbox.ExecOptions{Timeout: 10 * time.Second})
		done <- e
	}()
	observed := false
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); {
		req, _ := http.NewRequestWithContext(ctx, "GET", os.Getenv("AR_CLOUDFLARE_BRIDGE_URL")+"/v1/sandboxes/"+env.ID+"/transfer-grants/"+slowCap, nil)
		req.Header.Set("Authorization", "Bearer "+os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN"))
		req.Header.Set("X-Sandbox-Owner", conformance.TenantA().TenantId)
		req.Header.Set("User-Agent", "agentruntime-sandbox/1")
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal("transfer progress query failed")
		}
		var progress struct {
			Bytes  int64 `json:"bytes"`
			Active int   `json:"active"`
		}
		_ = json.NewDecoder(response.Body).Decode(&progress)
		response.Body.Close()
		if progress.Bytes > 0 && progress.Active > 0 {
			observed = true
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !observed {
		t.Fatal("did not observe active upload")
	}
	used = conformance.Must(network.RevokeTransferTarget(ctx, env.ID, slowCap))
	if used <= 0 || used >= prepared.SizeBytes {
		t.Fatalf("revoke did not interrupt upload: %d", used)
	}
	select {
	case <-done:
	case <-time.After(12 * time.Second):
		t.Fatal("revoked transfer still running")
	}
	if _, e = objects.Head(ctx, "slow"); e == nil {
		t.Fatal("revoked incomplete object was published")
	}
	if e = network.AcknowledgeTransferTarget(ctx, env.ID, slowCap); e != nil {
		t.Fatal(e)
	}
	_ = slowTarget
	t.Log("immutable PUT + bounded GET, actual byte counts, revoke, expiry, tenant isolation and delayed mint rejection verified; test objects/VM cleaned")
}
