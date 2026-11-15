package fcsandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

func validatePrepareURL(raw, prefix string) error {
	if raw == "" {
		return nil
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || prefix == "" {
		return errors.New("image_prepare_url must be an HTTP(S) service origin with a custom image prefix")
	}
	return nil
}

func (b *box) preparedImage(ctx context.Context, image string) (PreparedImage, error) {
	prefix := strings.TrimRight(b.p.cfg.CustomImagePrefix, "/") + "/"
	suffix := strings.TrimPrefix(image, prefix)
	if b.p.cfg.CustomImagePrefix == "" || !strings.HasPrefix(image, prefix+b.owner+"/env@sha256:") || !scopedImagePattern.MatchString(suffix) {
		return PreparedImage{}, providererr.New(providererr.Invalid, "unsupported_image", "Firecracker requires this tenant's immutable D17 image")
	}
	if prepared, ok := b.p.cfg.ImageRuntimes[image]; ok {
		return prepared, nil
	}
	if b.p.cfg.ImagePrepareURL == "" {
		return PreparedImage{}, providererr.New(providererr.Invalid, "unsupported_image", "Firecracker image has not been prepared")
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	data, _ := json.Marshal(map[string]string{"image": image})
	// Never follow a redirect carrying the private manager/preparer token.
	client := *b.p.client
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	for {
		requestCtx, done := context.WithTimeout(ctx, 15*time.Second)
		req, err := http.NewRequestWithContext(requestCtx, http.MethodPost, strings.TrimRight(b.p.cfg.ImagePrepareURL, "/")+"/v1/images/prepare", bytes.NewReader(data))
		if err != nil {
			done()
			return PreparedImage{}, providererr.New(providererr.Invalid, "image_prepare_invalid", "invalid image preparation endpoint")
		}
		for k, v := range b.p.headers(b.owner) {
			req.Header.Set(k, v)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			done()
			return PreparedImage{}, providererr.New(providererr.Transient, "image_prepare_unavailable", "Firecracker image preparation unavailable")
		}
		raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 8193))
		resp.Body.Close()
		done()
		var result struct {
			State        string `json:"state"`
			Image        string `json:"image"`
			Runtime      string `json:"runtime"`
			SourceDigest string `json:"source_digest"`
		}
		if readErr != nil || len(raw) > 8192 || json.Unmarshal(raw, &result) != nil {
			return PreparedImage{}, providererr.New(providererr.Transient, "image_prepare_bad_response", "invalid preparation response")
		}
		if resp.StatusCode == http.StatusOK && result.State == "ready" && result.Image == image && runtimePattern.MatchString(result.Runtime) && imageDigestPattern.MatchString(result.SourceDigest) {
			return PreparedImage{Runtime: result.Runtime, SourceDigest: result.SourceDigest}, nil
		}
		if resp.StatusCode != http.StatusAccepted || result.State != "preparing" || result.Image != image {
			return PreparedImage{}, providererr.New(providererr.Invalid, "image_prepare_failed", "Firecracker image preparation did not produce a verified rootfs")
		}
		timer := time.NewTimer(2 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return PreparedImage{}, providererr.New(providererr.Transient, "image_prepare_pending", "Firecracker image is still being prepared")
		case <-timer.C:
		}
	}
}
