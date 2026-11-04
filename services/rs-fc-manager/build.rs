// 真 VM 的用例（tests/real_vm.rs）只在 AR_TEST_FIRECRACKER=1 时编译：不设时不收集（docs/plan/firecracker-backend.md 第 6 节）。
fn main() {
    println!("cargo::rerun-if-env-changed=AR_TEST_FIRECRACKER");
    println!("cargo::rustc-check-cfg=cfg(ar_test_firecracker)");
    if std::env::var("AR_TEST_FIRECRACKER").as_deref() == Ok("1") {
        println!("cargo::rustc-cfg=ar_test_firecracker");
    }
}
