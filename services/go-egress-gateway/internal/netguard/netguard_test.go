package netguard

import (
	"net/netip"
	"testing"
)

func TestCheckBlocksInternalAddresses(t *testing.T) {
	cluster, err := ParseCIDRs("10.42.0.0/16, 10.43.0.0/16,198.19.0.0/16")
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"127.0.0.1":            "loopback",
		"10.0.0.5":             "private",
		"172.16.3.4":           "private",
		"192.168.1.1":          "private",
		"100.64.0.1":           "cgnat",
		"169.254.169.254":      "cloud_metadata",
		"169.254.1.1":          "link_local",
		"100.100.100.200":      "cloud_metadata",
		"0.0.0.0":              "unspecified",
		"224.0.0.1":            "multicast",
		"255.255.255.255":      "reserved",
		"198.19.5.5":           "cluster",
		"::1":                  "loopback",
		"::":                   "unspecified",
		"fe80::1":              "link_local",
		"fd00:ec2::254":        "cloud_metadata",
		"fc00::1":              "unique_local",
		"::ffff:127.0.0.1":     "loopback",
		"::ffff:169.254.169.254": "cloud_metadata",
		"64:ff9b::a9fe:a9fe":   "nat64_cloud_metadata",
		"64:ff9b::7f00:1":      "nat64_loopback",
		"2002:7f00:1::":        "tunnel",
		"2001:0:4136:e378::1":  "tunnel",
		"::7f00:1":             "ipv4_compatible",
		"2001:db8::1":          "documentation",
		"203.0.113.9":          "documentation",
	}
	for in, want := range cases {
		if got := Check(netip.MustParseAddr(in), cluster); got != want {
			t.Errorf("Check(%s) = %q, want %q", in, got, want)
		}
	}
	for _, ok := range []string{"93.184.216.34", "140.82.112.3", "2606:4700::6810:85e5", "64:ff9b::5db8:d822"} {
		if got := Check(netip.MustParseAddr(ok), cluster); got != "" {
			t.Errorf("Check(%s) = %q, want allowed", ok, got)
		}
	}
}

func TestLiteralIPRecognisesEveryNotation(t *testing.T) {
	cases := map[string]string{
		"127.0.0.1":        "127.0.0.1",
		"2130706433":       "127.0.0.1",
		"0x7f000001":       "127.0.0.1",
		"0x7f.1":           "127.0.0.1",
		"0177.0.0.1":       "127.0.0.1",
		"0177.1":           "127.0.0.1",
		"127.1":            "127.0.0.1",
		"127.0.1":          "127.0.0.1",
		"0251.0376.0251.0376": "169.254.169.254",
		"0xa9.0xfe.0xa9.0xfe": "169.254.169.254",
		"2852039166":       "169.254.169.254",
		"[::1]":            "::1",
		"::ffff:7f00:1":    "::ffff:127.0.0.1",
		"[fe80::1%eth0]":   "fe80::1",
		"10.0.0.1.":        "10.0.0.1",
	}
	for in, want := range cases {
		got, ok := LiteralIP(in)
		if !ok || got.WithZone("").String() != want {
			t.Errorf("LiteralIP(%q) = %v %v, want %s", in, got, ok, want)
		}
	}
	for _, name := range []string{"example.com", "api.github.com", "123.example.com", "0x7f.example", "deadbeef", "1.2.3.4.5", "256.1.1.1", "08.1.1.1"} {
		if a, ok := LiteralIP(name); ok {
			t.Errorf("LiteralIP(%q) = %v, want not an address", name, a)
		}
	}
}

func TestNormalizeHost(t *testing.T) {
	for in, want := range map[string]string{"API.Example.COM.": "api.example.com", "pypi.org": "pypi.org"} {
		if got, reason := NormalizeHost(in); got != want || reason != "" {
			t.Errorf("NormalizeHost(%q) = %q %q", in, got, reason)
		}
	}
	for in, want := range map[string]string{
		"2130706433":                       "ip_literal",
		"[::1]":                            "ip_literal",
		"0x7f.1":                           "ip_literal",
		"localhost":                        "internal_name",
		"kubernetes.default.svc":           "internal_name",
		"db.agent-runtime.svc.cluster.local": "internal_name",
		"metadata.google.internal":         "internal_name",
		"printer.local":                    "internal_name",
		"intranet":                         "internal_name",
		"exa_mple.com":                     "invalid_host",
		"-bad.com":                         "invalid_host",
		"a..b":                             "invalid_host",
		"":                                 "invalid_host",
	} {
		if _, reason := NormalizeHost(in); reason != want {
			t.Errorf("NormalizeHost(%q) reason = %q, want %q", in, reason, want)
		}
	}
}

func TestCovers(t *testing.T) {
	for _, c := range []struct {
		pattern, host string
		want          bool
	}{
		{"api.example.com", "api.example.com", true},
		{"api.example.com", "x.api.example.com", false},
		{"*.example.com", "a.example.com", true},
		{"*.example.com", "a.b.example.com", true},
		{"*.example.com", "example.com", false},
		{"*.example.com", "badexample.com", false},
		{"*.Example.com", "a.example.com", true},
	} {
		if got := Covers(c.pattern, c.host); got != c.want {
			t.Errorf("Covers(%q, %q) = %v", c.pattern, c.host, got)
		}
	}
}
