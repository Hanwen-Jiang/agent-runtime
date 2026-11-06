package registry

import (
	"context"
	"encoding/json"

	"github.com/agentruntime/agent-runtime/libs/go-core/registryref"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

// ProfileExport is a Gateway catalog with provenance read from Registry rows.
// Version is the file format; RegistryRevisions holds actual published versions.
type ProfileExport struct {
	Version           int                            `json:"version"`
	Profiles          []json.RawMessage              `json:"profiles"`
	RegistryRevisions map[string]registryref.Profile `json:"registry_revisions"`
}

// ExportModelProfiles reads default platform profiles in a single transaction.
// Exporting never publishes, promotes, or changes an existing Registry entry.
func (r *Registry) ExportModelProfiles(ctx context.Context) (ProfileExport, error) {
	scope, err := r.platformScope()
	if err != nil {
		return ProfileExport{}, err
	}
	entries, err := scope.defaults(ctx, registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE)
	if err != nil {
		return ProfileExport{}, err
	}
	out := ProfileExport{Version: 1, Profiles: []json.RawMessage{}, RegistryRevisions: map[string]registryref.Profile{}}
	for _, entry := range entries {
		doc := json.RawMessage(entry.document)
		hash, err := registryref.Digest(doc)
		if err != nil {
			return ProfileExport{}, err
		}
		out.Profiles = append(out.Profiles, doc)
		out.RegistryRevisions[entry.name] = registryref.Profile{Scope: "platform", Version: entry.label(), SHA256: hash}
	}
	return out, nil
}
