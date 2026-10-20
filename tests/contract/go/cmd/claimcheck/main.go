// Command claimcheck 用 Go 侧的 Claim-Check 编解码处理 Temporal Payload，供 tests/contract 的跨语言往返测试调用
// （计划 S-06）。ObjectStore 与 SecretStore 用开发实现，两种语言指向同一个目录与同一份密钥文件。
//
//	claimcheck encode --objects D --secrets F --tenant T --in P --out R [--threshold N]
//	claimcheck decode --objects D --secrets F --tenant T --in R --out P
//
// P、R 是 protojson 格式的 temporal.api.common.v1.Payload。失败时退出码 1，错误信息写到标准错误
// （第一行是统一错误模型的类别与错误码，形如 "denied claim_check_cross_tenant"）。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"

	commonpb "go.temporal.io/api/common/v1"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal/claimcheck"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func readPayload(path string) (*commonpb.Payload, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	p := &commonpb.Payload{}
	return p, protojson.Unmarshal(raw, p)
}

func writePayload(path string, p *commonpb.Payload) error {
	out, err := protojson.MarshalOptions{Multiline: true}.Marshal(p)
	if err != nil {
		return err
	}
	return os.WriteFile(path, out, 0o644)
}

func run(op string, args []string) error {
	fs := flag.NewFlagSet(op, flag.ExitOnError)
	objects := fs.String("objects", "", "ObjectStore 开发实现的根目录")
	secrets := fs.String("secrets", "", "SecretStore 开发实现的密钥文件")
	tenant := fs.String("tenant", "", "租户 ID")
	in := fs.String("in", "", "输入的 Payload（protojson）")
	out := fs.String("out", "", "输出的 Payload（protojson）")
	threshold := fs.Int("threshold", 0, "阈值（字节），0 表示默认值")
	_ = fs.Parse(args)
	if *objects == "" || *secrets == "" || *tenant == "" || *in == "" || *out == "" {
		return errors.New("缺少参数")
	}
	objs, err := diskobjects.New(diskobjects.Options{Root: *objects, SigningKey: []byte("crosslang")})
	if err != nil {
		return err
	}
	secs, err := filesecrets.New(filesecrets.Options{Path: *secrets})
	if err != nil {
		return err
	}
	cc, err := claimcheck.New(claimcheck.Options{Objects: objs, Secrets: secs, Threshold: *threshold})
	if err != nil {
		return err
	}
	codec, err := cc.Bind(&commonv1.TenantContext{TenantId: *tenant})
	if err != nil {
		return err
	}
	p, err := readPayload(*in)
	if err != nil {
		return err
	}
	var result []*commonpb.Payload
	if op == "encode" {
		result, err = codec.Encode(context.Background(), []*commonpb.Payload{p})
	} else {
		result, err = codec.Decode(context.Background(), []*commonpb.Payload{p})
	}
	if err != nil {
		return err
	}
	return writePayload(*out, result[0])
}

func main() {
	if len(os.Args) < 2 || (os.Args[1] != "encode" && os.Args[1] != "decode") {
		fmt.Fprintln(os.Stderr, "usage: claimcheck encode|decode --objects D --secrets F --tenant T --in P --out R [--threshold N]")
		os.Exit(2)
	}
	if err := run(os.Args[1], os.Args[2:]); err != nil {
		var pe *providererr.Error
		if errors.As(err, &pe) {
			fmt.Fprintf(os.Stderr, "%s %s\n", categoryName(pe.Category), pe.Code)
		}
		fmt.Fprintln(os.Stderr, "claimcheck:", err)
		os.Exit(1)
	}
}

func categoryName(c providererr.Category) string {
	switch c {
	case providererr.Transient:
		return "transient"
	case providererr.Invalid:
		return "invalid"
	case providererr.Denied:
		return "denied"
	case providererr.NotFound:
		return "not_found"
	case providererr.Conflict:
		return "conflict"
	default:
		return "unknown"
	}
}
