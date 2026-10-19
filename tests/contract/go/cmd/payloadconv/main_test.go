package main

import (
	"path/filepath"
	"testing"

	"google.golang.org/protobuf/proto"
)

// TestGoRoundTrip 先在 Go 内部往返一遍，跨语言测试失败时能区分是哪一侧的问题。
func TestGoRoundTrip(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join("..", "..", "..", "fixtures", "crosslang", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatal("no fixtures in tests/contract/fixtures/crosslang")
	}
	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			want, err := loadFixture(path)
			if err != nil {
				t.Fatal(err)
			}
			payload, err := dataConverter().ToPayload(want)
			if err != nil {
				t.Fatal(err)
			}
			got, err := decode(payload, want)
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(want, got) {
				t.Fatalf("round trip changed the message\nwant: %v\ngot:  %v", want, got)
			}
		})
	}
}
