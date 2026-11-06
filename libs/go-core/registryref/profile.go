// Package registryref identifies the immutable Registry entry that supplied a
// model profile. Versions are per entry, never a fabricated global revision.
package registryref

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
)

// Profile is exported alongside, not inside, the Registry profile document.
// Together with the profile ID it names platform/model_profile/<id>@<version>.
type Profile struct {
	Scope   string `json:"scope"`
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
}

var version = regexp.MustCompile(`^v[1-9][0-9]*$`)
var digest = regexp.MustCompile(`^[0-9a-f]{64}$`)

func (p Profile) Validate() error {
	if p.Scope != "platform" || !version.MatchString(p.Version) || !digest.MatchString(p.SHA256) {
		return fmt.Errorf("invalid model profile Registry reference")
	}
	return nil
}

// Digest normalizes JSON key order/whitespace without losing number precision.
func Digest(document []byte) (string, error) {
	dec := json.NewDecoder(bytes.NewReader(document))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return "", err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return "", fmt.Errorf("profile must contain one JSON document")
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}
