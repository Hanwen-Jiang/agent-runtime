# 运行环境的样例项目（D-14）

`tests/deploy/test_images.py` 设 `AR_TEST_IMAGES=1` 时在 `sandbox-universal` 镜像里（断网、只读根文件系统、以 10001 运行）编译并测试这五个项目，证明五种语言的工具链都能用。都没有外部依赖，离线就能跑。
