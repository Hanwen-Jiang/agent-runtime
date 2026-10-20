// Command payloadconv 用 Temporal Go SDK 的 DataConverter 编解码 Runtime 的工作流载荷，
// 供 tests/contract 的跨语言往返测试调用（计划 S-01）。
//
//	payloadconv encode --fixture F --out P     读取 fixture，编码为 Temporal Payload，以 protojson 写出
//	payloadconv verify --payload P --fixture F  解码 Payload，与 fixture 用 proto.Equal 比较
//
// fixture 格式见 tests/contract/fixtures/crosslang/README.md。
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"reflect"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/converter"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"

	// 把 Runtime 的全部消息类型注册到全局 registry，fixture 按消息全名查找类型。
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/memory/v1"
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	_ "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

const encodingJSONProtobuf = "json/protobuf"

// dataConverter 返回 Runtime 使用的载荷转换：SDK 默认值，proto 消息优先编码为 json/protobuf（两个服务都是这样）。
// 服务的 DataConverter 另外接了 Claim-Check（S-06），只改动超过阈值的载荷，这里的 fixture 都在阈值以下；
// Claim-Check 自己的跨语言往返见 cmd/claimcheck。
func dataConverter() converter.DataConverter {
	return converter.GetDefaultDataConverter()
}

type fixture struct {
	Type    string          `json:"type"`
	Message json.RawMessage `json:"message"`
}

func loadFixture(path string) (proto.Message, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var fx fixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	mt, err := protoregistry.GlobalTypes.FindMessageByName(protoreflect.FullName(fx.Type))
	if err != nil {
		return nil, fmt.Errorf("%s: unknown message type %q: %w", path, fx.Type, err)
	}
	msg := mt.New().Interface()
	if err := protojson.Unmarshal(fx.Message, msg); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return msg, nil
}

// decode 把 payload 解码成与 like 同类型的新消息。
func decode(payload *commonpb.Payload, like proto.Message) (proto.Message, error) {
	wantType := string(like.ProtoReflect().Descriptor().FullName())
	if got := string(payload.GetMetadata()["encoding"]); got != encodingJSONProtobuf {
		return nil, fmt.Errorf("encoding = %q, want %q", got, encodingJSONProtobuf)
	}
	if got := string(payload.GetMetadata()["messageType"]); got != wantType {
		return nil, fmt.Errorf("messageType = %q, want %q", got, wantType)
	}
	// FromPayload 需要指向消息指针的指针，例如 **TaskWorkflowInput。
	target := reflect.New(reflect.TypeOf(like))
	if err := dataConverter().FromPayload(payload, target.Interface()); err != nil {
		return nil, err
	}
	msg, ok := target.Elem().Interface().(proto.Message)
	if !ok || msg == nil {
		return nil, errors.New("decoded value is not a proto message")
	}
	return msg, nil
}

func encode(fixturePath, outPath string) error {
	msg, err := loadFixture(fixturePath)
	if err != nil {
		return err
	}
	payload, err := dataConverter().ToPayload(msg)
	if err != nil {
		return err
	}
	out, err := protojson.MarshalOptions{Multiline: true}.Marshal(payload)
	if err != nil {
		return err
	}
	return os.WriteFile(outPath, out, 0o644)
}

func verify(payloadPath, fixturePath string) error {
	want, err := loadFixture(fixturePath)
	if err != nil {
		return err
	}
	raw, err := os.ReadFile(payloadPath)
	if err != nil {
		return err
	}
	payload := &commonpb.Payload{}
	if err := protojson.Unmarshal(raw, payload); err != nil {
		return fmt.Errorf("%s: %w", payloadPath, err)
	}
	got, err := decode(payload, want)
	if err != nil {
		return err
	}
	if !proto.Equal(want, got) {
		return fmt.Errorf("decoded message differs from fixture\nwant: %s\ngot:  %s",
			protojson.Format(want), protojson.Format(got))
	}
	return nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage:")
	fmt.Fprintln(os.Stderr, "  payloadconv encode --fixture F --out P")
	fmt.Fprintln(os.Stderr, "  payloadconv verify --payload P --fixture F")
	os.Exit(2)
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "encode":
		fs := flag.NewFlagSet("encode", flag.ExitOnError)
		fixturePath := fs.String("fixture", "", "fixture file")
		outPath := fs.String("out", "", "payload output file")
		_ = fs.Parse(os.Args[2:])
		if *fixturePath == "" || *outPath == "" {
			usage()
		}
		err = encode(*fixturePath, *outPath)
	case "verify":
		fs := flag.NewFlagSet("verify", flag.ExitOnError)
		payloadPath := fs.String("payload", "", "payload file")
		fixturePath := fs.String("fixture", "", "fixture file")
		_ = fs.Parse(os.Args[2:])
		if *payloadPath == "" || *fixturePath == "" {
			usage()
		}
		err = verify(*payloadPath, *fixturePath)
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "payloadconv:", err)
		os.Exit(1)
	}
}
