package broker

import (
	"context"
	"testing"
)

func TestTransferGrantRequiresProviderAccountOrBucketHostname(t *testing.T) {
	for _, host := range []string{
		"0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
		"0123456789abcdef0123456789abcdef.eu.r2.cloudflarestorage.com",
		"0123456789abcdef0123456789abcdef.fedramp.r2.cloudflarestorage.com",
		"example-bucket.s3.amazonaws.com", "example-bucket.s3.us-west-2.amazonaws.com", "example-bucket.s3-us-west-2.amazonaws.com",
	} {
		if !scopedTransferHost(host) {
			t.Errorf("valid scoped storage: %s", host)
		}
	}
	for _, host := range []string{
		"s3.amazonaws.com", "s3.us-west-2.amazonaws.com", "s3-us-west-2.amazonaws.com", "r2.cloudflarestorage.com",
		"unproven.r2.cloudflarestorage.com", "example.com", "bucket.s3.amazonaws.com.evil.test", "bucket..name.s3.amazonaws.com", "bucket.s3.amazonaws.com.",
	} {
		if scopedTransferHost(host) {
			t.Errorf("unscoped storage: %s", host)
		}
		// Must reject before accessing the grant ledger or attempting any network.
		_, _, err := (&Broker{}).OpenTransferNetwork(context.Background(), &Lease{Kind: "k8s"}, "https://"+host+"/another-bucket/object?signature=redacted")
		if err == nil {
			t.Errorf("grant accepted %s", host)
		}
	}
}
