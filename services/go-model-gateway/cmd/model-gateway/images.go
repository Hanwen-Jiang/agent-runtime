package main

import (
	"crypto/rand"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
)

// Reuse exactly the task-service / worker store. An unconfigured gateway remains
// text-only rather than opening an unrelated local store by default.
func imageObjects(getenv func(string) string) (objectstore.Provider, error) {
	opts, enabled, err := s3objects.OptionsFromEnv(getenv)
	if err != nil {
		return nil, err
	}
	if enabled {
		return s3objects.New(opts)
	}
	root := strings.TrimSpace(getenv("AR_OBJECT_STORE_DIR"))
	if root == "" {
		return nil, nil
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	return diskobjects.New(diskobjects.Options{Root: root, SigningKey: key})
}
