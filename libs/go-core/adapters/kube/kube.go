// Package kube 是一个很小的 Kubernetes API 客户端（只用标准库）：Sandbox Service 的 K8s 后端建、查、删沙箱 Pod 与构建
// 项目环境的 Job 用（D-05、D-17；SBX-01 从 Python 的 agentruntime_core.adapters.kube 搬来）。不引 client-go（设计书 4.15）。
//
// 连接方式：
//   - 集群内：ServiceAccount 的令牌与 CA（/var/run/secrets/kubernetes.io/serviceaccount），地址取 KUBERNETES_SERVICE_HOST、_PORT；
//     令牌每次请求重新读（kubelet 会轮换它）。
//   - 集群外（开发、测试）：JSON 格式的 kubeconfig（kubectl config view --raw --minify -o json），支持 CA、客户端证书与令牌。
//
// 错误：404 是 not_found；409 是 conflict；400、422 是 invalid；401、403 是 denied；连不上、429、5xx 是 transient。
package kube

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// ServiceAccount 是集群内的 ServiceAccount 目录。
const ServiceAccount = "/var/run/secrets/kubernetes.io/serviceaccount"

// API 是一个 API Server 的客户端。
type API struct {
	Server    string
	client    *http.Client
	token     string
	tokenFile string
}

// Load 给了 kubeconfig 就用它；否则在集群内用 ServiceAccount。
func Load(kubeconfig string) (*API, error) {
	if kubeconfig != "" {
		return FromKubeconfig(kubeconfig)
	}
	return InCluster()
}

// InCluster 用 ServiceAccount 连本集群。
func InCluster() (*API, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if port == "" {
		port = "443"
	}
	tokenFile := filepath.Join(ServiceAccount, "token")
	if _, err := os.Stat(tokenFile); host == "" || err != nil {
		return nil, errors.New("不在集群里（没有 KUBERNETES_SERVICE_HOST 或 ServiceAccount 令牌），要给 kubeconfig")
	}
	ca, err := os.ReadFile(filepath.Join(ServiceAccount, "ca.crt"))
	if err != nil {
		return nil, fmt.Errorf("读不了 ServiceAccount 的 CA：%w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("ServiceAccount 的 CA 格式不对")
	}
	return &API{Server: "https://" + net.JoinHostPort(host, port), client: newClient(&tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}), tokenFile: tokenFile}, nil
}

type kubeconfig struct {
	CurrentContext string `json:"current-context"`
	Contexts       []struct {
		Name    string `json:"name"`
		Context struct {
			Cluster string `json:"cluster"`
			User    string `json:"user"`
		} `json:"context"`
	} `json:"contexts"`
	Clusters []struct {
		Name    string `json:"name"`
		Cluster struct {
			Server                   string `json:"server"`
			CertificateAuthorityData string `json:"certificate-authority-data"`
			CertificateAuthority     string `json:"certificate-authority"`
		} `json:"cluster"`
	} `json:"clusters"`
	Users []struct {
		Name string `json:"name"`
		User struct {
			ClientCertificateData string `json:"client-certificate-data"`
			ClientKeyData         string `json:"client-key-data"`
			ClientCertificate     string `json:"client-certificate"`
			ClientKey             string `json:"client-key"`
			Token                 string `json:"token"`
		} `json:"user"`
	} `json:"users"`
}

// FromKubeconfig 读 JSON 格式的 kubeconfig。
func FromKubeconfig(path string) (*API, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读不了 kubeconfig：%w", err)
	}
	var cfg kubeconfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, errors.New("kubeconfig 要是 JSON 格式（kubectl config view --raw --minify -o json）")
	}
	var clusterName, userName string
	for _, c := range cfg.Contexts {
		if c.Name == cfg.CurrentContext {
			clusterName, userName = c.Context.Cluster, c.Context.User
		}
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	server := ""
	for _, c := range cfg.Clusters {
		if c.Name != clusterName {
			continue
		}
		server = c.Cluster.Server
		var ca []byte
		switch {
		case c.Cluster.CertificateAuthorityData != "":
			ca, err = base64.StdEncoding.DecodeString(c.Cluster.CertificateAuthorityData)
		case c.Cluster.CertificateAuthority != "":
			ca, err = os.ReadFile(c.Cluster.CertificateAuthority)
		}
		if err != nil {
			return nil, fmt.Errorf("kubeconfig 的 CA 装不上：%w", err)
		}
		if ca != nil {
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(ca) {
				return nil, errors.New("kubeconfig 的 CA 格式不对")
			}
			tlsConfig.RootCAs = pool
		}
	}
	if !strings.HasPrefix(server, "https://") {
		return nil, errors.New("kubeconfig 的 server 要是 https 地址")
	}
	token := ""
	for _, u := range cfg.Users {
		if u.Name != userName {
			continue
		}
		token = u.User.Token
		var certPEM, keyPEM []byte
		switch {
		case u.User.ClientCertificateData != "" && u.User.ClientKeyData != "":
			certPEM, err = base64.StdEncoding.DecodeString(u.User.ClientCertificateData)
			if err == nil {
				keyPEM, err = base64.StdEncoding.DecodeString(u.User.ClientKeyData)
			}
		case u.User.ClientCertificate != "" && u.User.ClientKey != "":
			certPEM, err = os.ReadFile(u.User.ClientCertificate)
			if err == nil {
				keyPEM, err = os.ReadFile(u.User.ClientKey)
			}
		}
		if err != nil {
			return nil, fmt.Errorf("kubeconfig 的客户端证书装不上：%w", err)
		}
		if certPEM != nil {
			pair, err := tls.X509KeyPair(certPEM, keyPEM)
			if err != nil {
				return nil, fmt.Errorf("kubeconfig 的客户端证书装不上：%w", err)
			}
			tlsConfig.Certificates = []tls.Certificate{pair}
		}
	}
	return &API{Server: strings.TrimRight(server, "/"), client: newClient(tlsConfig), token: token}, nil
}

func newClient(cfg *tls.Config) *http.Client {
	return &http.Client{Transport: &http.Transport{Proxy: nil, TLSClientConfig: cfg, MaxIdleConnsPerHost: 16, IdleConnTimeout: 90 * time.Second}}
}

// HTTPClient 是带着 API Server 的 TLS 配置的客户端（经 pods/proxy 连 agent 时用）。
func (a *API) HTTPClient() *http.Client { return a.client }

// AuthHeaders 是认证头（令牌每次重新读）。
func (a *API) AuthHeaders() map[string]string {
	token := a.token
	if a.tokenFile != "" {
		if raw, err := os.ReadFile(a.tokenFile); err == nil {
			token = strings.TrimSpace(string(raw))
		}
	}
	if token == "" {
		return map[string]string{}
	}
	return map[string]string{"Authorization": "Bearer " + token}
}

// PodProxyURL 是经 API Server 转发到 Pod 端口的地址（pods/proxy），后面接路径。
func (a *API) PodProxyURL(namespace, pod string, port int) string {
	return fmt.Sprintf("%s/api/v1/namespaces/%s/pods/%s:%d/proxy", a.Server, namespace, pod, port)
}

// Request 发一个请求，返回状态码与响应体；body 是 []byte 时原样发，其余按 JSON 编码。
func (a *API) Request(ctx context.Context, method, path string, body any, contentType string) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		data, ok := body.([]byte)
		if !ok {
			var err error
			if data, err = json.Marshal(body); err != nil {
				return 0, nil, providererr.Wrap(providererr.Invalid, "kube_invalid", "请求体编码不了", err)
			}
		}
		reader = bytes.NewReader(data)
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, a.Server+path, reader)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Invalid, "kube_invalid", "请求地址不对", err)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		if contentType == "" {
			contentType = "application/json"
		}
		req.Header.Set("Content-Type", contentType)
	}
	for k, v := range a.AuthHeaders() {
		req.Header.Set(k, v)
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "kube_unavailable", "连不上 Kubernetes API", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "kube_unavailable", "Kubernetes API 的传输中断了", err)
	}
	return resp.StatusCode, data, nil
}

// JSON 发一个请求，2xx 时把响应解进 out（可以为 nil），否则按 ErrorFor 报错。
func (a *API) JSON(ctx context.Context, method, path string, body any, out any) error {
	return a.JSONType(ctx, method, path, body, "", out)
}

// JSONType 同 JSON，另给请求体的类型（merge-patch 等）。
func (a *API) JSONType(ctx context.Context, method, path string, body any, contentType string, out any) error {
	status, data, err := a.Request(ctx, method, path, body, contentType)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return ErrorFor(status, data)
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return providererr.Wrap(providererr.Transient, "kube_bad_response", "Kubernetes API 的响应格式不对", err)
		}
	}
	return nil
}

// ErrorFor 把 API Server 的错误响应变成统一错误模型。
func ErrorFor(status int, body []byte) error {
	var e struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &e)
	msg := e.Message
	if len(msg) > 300 {
		msg = msg[:300]
	}
	or := func(fallback string) string {
		if msg != "" {
			return msg
		}
		return fallback
	}
	switch status {
	case 404:
		return providererr.New(providererr.NotFound, "kube_not_found", or("不存在"))
	case 409:
		return providererr.New(providererr.Conflict, "kube_conflict", or("冲突"))
	case 400, 422:
		return providererr.New(providererr.Invalid, "kube_invalid", or("Kubernetes 拒绝了这个请求"))
	case 401, 403:
		return providererr.New(providererr.Denied, "kube_denied", or("没有权限"))
	default:
		return providererr.New(providererr.Transient, "kube_unavailable", or(fmt.Sprintf("Kubernetes API 暂时不可用（HTTP %d）", status)))
	}
}

// LabelSelector 把标签写成查询参数的值（按名字排序，已转义）。
func LabelSelector(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+labels[k])
	}
	return url.QueryEscape(strings.Join(parts, ","))
}
