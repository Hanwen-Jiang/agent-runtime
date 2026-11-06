package config

// 约定 4：model-gateway 的环境变量与默认值；配置缺必填项时报错，错误信息只提变量名，不带取值（取值可能是密钥或带凭据的地址）。

import (
	"strings"
	"testing"
)

const leaky = "sk-test-DO-NOT-LEAK-0123"

func env(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func base() map[string]string {
	return map[string]string{
		EnvProfilesFile:    "profiles.json",
		EnvProviderBaseURL: "https://models.example.test/v1",
		EnvSecretsFile:     "secrets.json",
	}
}

func TestFromEnvDefaults(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	want := Config{
		DataDir:         "data",
		Addr:            "127.0.0.1:8091",
		ProfilesFile:    "profiles.json",
		Provider:        "openai_compat",
		ProviderBaseURL: "https://models.example.test/v1",
		ProviderKeyName: "model-provider/api-key",
		SecretsFile:     "secrets.json",
	}
	if cfg != want {
		t.Errorf("默认值：\n got %+v\nwant %+v", cfg, want)
	}
}

func TestFromEnvReadsAllVariables(t *testing.T) {
	m := map[string]string{
		EnvAddr:               "0.0.0.0:9000",
		EnvProfilesFile:       "/etc/ar/profiles.json",
		EnvProvider:           "openai_compat",
		EnvProviderBaseURL:    "http://127.0.0.1:5001/v1",
		EnvProviderKeyName:    "vendor/key",
		EnvSecretsFile:        "/var/ar/secrets.json",
		EnvTenantPoliciesFile: "/etc/ar/tenants.json",
	}
	cfg, err := FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	want := Config{
		DataDir: "data",
		Addr:    "0.0.0.0:9000", ProfilesFile: "/etc/ar/profiles.json", Provider: "openai_compat",
		ProviderBaseURL: "http://127.0.0.1:5001/v1", ProviderKeyName: "vendor/key",
		SecretsFile: "/var/ar/secrets.json", TenantPoliciesFile: "/etc/ar/tenants.json",
	}
	if cfg != want {
		t.Errorf("环境变量没有读进来：\n got %+v\nwant %+v", cfg, want)
	}
}

func TestMockProviderNeedsNoEndpointOrSecrets(t *testing.T) {
	cfg, err := FromEnv(env(map[string]string{EnvProfilesFile: "profiles.json", EnvProvider: "mock"}))
	if err != nil {
		t.Fatalf("mock 不要求端点与密钥文件：%v", err)
	}
	if cfg.Provider != ProviderMock {
		t.Errorf("Provider = %q", cfg.Provider)
	}
}

func TestRequireRegistryConfiguration(t *testing.T) {
	m := base()
	m[EnvRequireRegistry] = "true"
	cfg, err := FromEnv(env(m))
	if err != nil || !cfg.RequireRegistry {
		t.Fatalf("require Registry not enabled: %+v %v", cfg, err)
	}
	m[EnvRequireRegistry] = "typo"
	if _, err := FromEnv(env(m)); err == nil {
		t.Fatal("invalid provenance enforcement option accepted")
	}
}

func TestFromEnvRejectsMissingOrBadValues(t *testing.T) {
	cases := map[string]struct {
		mutate  func(m map[string]string)
		mention string
	}{
		"缺 Profile 文件":        {func(m map[string]string) { delete(m, EnvProfilesFile) }, EnvProfilesFile},
		"openai_compat 缺端点":   {func(m map[string]string) { delete(m, EnvProviderBaseURL) }, EnvProviderBaseURL},
		"openai_compat 缺密钥文件": {func(m map[string]string) { delete(m, EnvSecretsFile) }, EnvSecretsFile},
		"端点不是 http(s)":        {func(m map[string]string) { m[EnvProviderBaseURL] = "ftp://models.example.test/v1" }, EnvProviderBaseURL},
		"端点没有主机名":             {func(m map[string]string) { m[EnvProviderBaseURL] = "https:///v1" }, EnvProviderBaseURL},
		"端点带凭据且不合法":           {func(m map[string]string) { m[EnvProviderBaseURL] = "ftp://u:" + leaky + "@h/v1" }, EnvProviderBaseURL},
		"未知的供应商":              {func(m map[string]string) { m[EnvProvider] = leaky }, EnvProvider},
	}
	for name, c := range cases {
		m := base()
		c.mutate(m)
		_, err := FromEnv(env(m))
		if err == nil {
			t.Errorf("%s：应报错", name)
			continue
		}
		if !strings.Contains(err.Error(), c.mention) {
			t.Errorf("%s：错误信息应指出变量 %s，实际 %q", name, c.mention, err)
		}
		if strings.Contains(err.Error(), leaky) {
			t.Errorf("%s：错误信息不得带取值：%q", name, err)
		}
	}
}

func TestSecretsFileForDevSecret(t *testing.T) {
	path, err := SecretsFile(env(map[string]string{EnvSecretsFile: "/tmp/secrets.json"}))
	if err != nil || path != "/tmp/secrets.json" {
		t.Errorf("SecretsFile = %q, %v", path, err)
	}
	if _, err := SecretsFile(env(nil)); err == nil || !strings.Contains(err.Error(), EnvSecretsFile) {
		t.Errorf("缺 %s 时应报错并指出变量名：%v", EnvSecretsFile, err)
	}
}
