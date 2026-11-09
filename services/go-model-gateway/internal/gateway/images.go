package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"regexp"
	"strings"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
)

const maxImages = 10
const maxImageBytes = 10 << 20
const maxTotalImageBytes = 20 << 20
const maxImagePixels = 20_000_000
const maxImageEdge = 8192

var imageTaskID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
var imageSHA = regexp.MustCompile(`^[a-f0-9]{64}$`)
var imageMIMEs = []string{"image/png", "image/jpeg", "image/webp"}

func (s *Server) GetCapabilities(ctx context.Context, req *connect.Request[mg.GetCapabilitiesRequest]) (*connect.Response[mg.GetCapabilitiesResponse], error) {
	if err := checkTenant(req.Msg.GetTenant()); err != nil {
		return nil, connecterr.Error(err)
	}
	reserve, err := s.opts.Router.ImageTokenReserve(ctx, req.Msg.GetTenant().GetTenantId())
	if err != nil {
		return nil, connecterr.Error(err)
	}
	return connect.NewResponse(&mg.GetCapabilitiesResponse{ImageInput: s.opts.Objects != nil && reserve > 0, ImageMimeTypes: imageMIMEs, MaxImages: maxImages, MaxImageBytes: maxImageBytes, MaxImagePixels: maxImagePixels, ImageTokenReserve: int32(reserve)}), nil
}
func hasImages(req modelprovider.GenerateRequest) bool {
	for _, m := range req.Messages {
		for _, p := range m.Parts {
			if p.Image != nil {
				return true
			}
		}
	}
	return false
}

// Only the trusted task-service pin grants access. Tenant membership alone is
// insufficient: another task's uploaded bytes may not be read by this request.
func (s *Server) resolveImages(ctx context.Context, tenant *commonv1.TenantContext, call *mg.CallContext, req *modelprovider.GenerateRequest) error {
	if !hasImages(*req) {
		return nil
	}
	if s.opts.Objects == nil {
		return invalid("image_input_unsupported", "image input is not configured")
	}
	if !imageTaskID.MatchString(call.GetTaskId()) || tenant.GetWorkspaceId() == "" {
		return invalid("invalid_image_scope", "image input requires a task and workspace")
	}
	store, err := s.opts.Objects.Bind(tenant)
	if err != nil {
		return err
	}
	pin, err := store.Get(ctx, "task-inputs/"+call.GetTaskId()+"/manifest.json")
	if err != nil {
		return providererr.New(providererr.Denied, "image_not_authorized", "image input is not authorized for this task")
	}
	var manifest struct {
		Attachments []string `json:"attachments"`
		WorkspaceID string   `json:"workspace_id"`
	}
	if len(pin.Data) > 64<<10 || json.Unmarshal(pin.Data, &manifest) != nil || manifest.WorkspaceID != tenant.GetWorkspaceId() {
		return invalid("invalid_image_manifest", "invalid task image manifest")
	}
	allowed := map[string]bool{}
	for _, uri := range manifest.Attachments {
		allowed[uri] = true
	}
	count, total := 0, 0
	for mi := range req.Messages {
		for pi := range req.Messages[mi].Parts {
			p := &req.Messages[mi].Parts[pi]
			im := p.Image
			if im == nil {
				continue
			}
			count++
			if count > maxImages {
				return invalid("image_limit", "too many images")
			}
			if !allowed[im.ObjectURI] || !strings.HasPrefix(im.ObjectURI, "obj://"+tenant.GetTenantId()+"/uploads/src_") {
				return providererr.New(providererr.Denied, "image_not_authorized", "image input is not authorized for this task")
			}
			key, err := store.KeyOf(im.ObjectURI)
			if err != nil {
				return err
			}
			info, err := store.Head(ctx, key)
			if err != nil {
				return err
			}
			if info.Size <= 0 || info.Size > maxImageBytes || info.Size > int64(maxTotalImageBytes-total) {
				return invalid("image_limit", "image payload exceeds application limits")
			}
			if !imageSHA.MatchString(im.SHA256) || info.SHA256 != im.SHA256 || info.Metadata["workspace_id"] != tenant.GetWorkspaceId() {
				return invalid("image_mismatch", "image metadata does not match its reference")
			}
			data, err := store.Get(ctx, key)
			if err != nil {
				return err
			}
			sum := sha256.Sum256(data.Data)
			if int64(len(data.Data)) != info.Size || hex.EncodeToString(sum[:]) != im.SHA256 {
				return invalid("image_mismatch", "image content failed integrity validation")
			}
			mime, w, h, err := imageDimensions(data.Data)
			if err != nil {
				return err
			}
			if mime != im.MIMEType || mime != info.ContentType {
				return invalid("image_mismatch", "image MIME type does not match its bytes")
			}
			if w < 1 || h < 1 || w > maxImageEdge || h > maxImageEdge || int64(w)*int64(h) > maxImagePixels {
				return invalid("image_limit", "image dimensions exceed application limits")
			}
			im.Data = data.Data
			im.Width = w
			im.Height = h
			total += len(data.Data)
		}
	}
	return nil
}

func imageDimensions(data []byte) (string, int, int, error) {
	if len(data) >= 12 && string(data[:4]) == "RIFF" && string(data[8:12]) == "WEBP" {
		// Read the bounded WebP container header without decoding a potentially huge
		// pixel buffer. Animation is not part of this version's input contract.
		if int64(binary.LittleEndian.Uint32(data[4:8]))+8 != int64(len(data)) {
			return "", 0, 0, invalid("invalid_image", "invalid WebP container")
		}
		width, height := 0, 0
		frame := false
		extended := false
		off := 12
		for off+8 <= len(data) {
			size := int64(binary.LittleEndian.Uint32(data[off+4 : off+8]))
			end := int64(off) + 8 + size
			if end > int64(len(data)) {
				return "", 0, 0, invalid("invalid_image", "truncated WebP chunk")
			}
			chunk := data[off+8 : int(end)]
			switch string(data[off : off+4]) {
			case "VP8X":
				if off != 12 || extended || frame || len(chunk) != 10 || chunk[0]&2 != 0 {
					return "", 0, 0, invalid("invalid_image", "animated or invalid WebP is unsupported")
				}
				extended = true
				width = 1 + int(chunk[4]) + int(chunk[5])<<8 + int(chunk[6])<<16
				height = 1 + int(chunk[7]) + int(chunk[8])<<8 + int(chunk[9])<<16
			case "VP8 ":
				if frame || len(chunk) < 10 || !bytes.Equal(chunk[3:6], []byte{0x9d, 1, 0x2a}) {
					return "", 0, 0, invalid("invalid_image", "invalid WebP frame")
				}
				w, h := int(binary.LittleEndian.Uint16(chunk[6:8])&0x3fff), int(binary.LittleEndian.Uint16(chunk[8:10])&0x3fff)
				if width == 0 {
					width, height = w, h
				} else if w != width || h != height {
					return "", 0, 0, invalid("invalid_image", "WebP frame dimensions disagree")
				}
				frame = true
			case "VP8L":
				if frame || len(chunk) < 5 || chunk[0] != 0x2f {
					return "", 0, 0, invalid("invalid_image", "invalid lossless WebP frame")
				}
				bits := binary.LittleEndian.Uint32(chunk[1:5])
				if bits>>29 != 0 {
					return "", 0, 0, invalid("invalid_image", "unsupported WebP lossless version")
				}
				w, h := int(bits&0x3fff)+1, int((bits>>14)&0x3fff)+1
				if width == 0 {
					width, height = w, h
				} else if w != width || h != height {
					return "", 0, 0, invalid("invalid_image", "WebP frame dimensions disagree")
				}
				frame = true
			case "ANIM", "ANMF":
				return "", 0, 0, invalid("invalid_image", "animated WebP is unsupported")
			}
			if size&1 != 0 && (end >= int64(len(data)) || data[int(end)] != 0) {
				return "", 0, 0, invalid("invalid_image", "invalid WebP chunk padding")
			}
			off = int(end) + (int(size) & 1)
		}
		if off != len(data) {
			return "", 0, 0, invalid("invalid_image", "invalid WebP chunk padding")
		}
		if frame {
			return "image/webp", width, height, nil
		}
		return "", 0, 0, invalid("invalid_image", "WebP contains no frame")
	}
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || (format != "png" && format != "jpeg") {
		return "", 0, 0, invalid("invalid_image", "only valid PNG, JPEG and static WebP images are supported")
	}
	return "image/" + format, cfg.Width, cfg.Height, nil
}
