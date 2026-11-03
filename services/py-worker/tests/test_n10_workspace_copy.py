"""N-10（docs/plan/feedback-2026-11-01.md）：新会话的工作副本不复制整个仓库，会话之间仍然隔离。

- 挂卷：在卷上复制，.git/objects 的文件是硬链接（同一个 inode），工作树、index、refs 各自一份；未提交的改动与未跟踪的文件照样在。
  副本里提交、源里 gc 都不影响对方（git fsck 通过）。
- 存档：新工作区的快照引用源的块，不搬内容、不起环境；改了的文件才写进新工作区自己的块。源删除或清理旧快照时，副本还引用的块留着；
  副本也不用了之后才删。
- 记下实测的差别（复制的字节与硬链接的字节、写进 ObjectStore 的块数）。
"""

from __future__ import annotations

from pathlib import Path

from .test_workspace_operations import Op, Setup, git, make_remote


def _inodes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_ino for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


async def _base(s: Setup, url: str) -> None:
    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-base", repo_url=url)
    await s.tool("repo_edit", {"path": "draft.md", "new": "not committed yet\n"}, workspace="w-base")


async def test_mount_mode_links_objects_and_keeps_sessions_isolated(tmp_path: Path) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode="mount")
    await _base(s, url)
    base, copy = s.host / "t1" / "w-base", s.host / "t1" / "w-copy"
    copied = (await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy", from_workspace_id="w-base"))["workspace"]
    assert copied["head"] == git("rev-parse", "HEAD", cwd=base) and copied["branch"] == "main"
    assert (copy / "draft.md").read_text() == "not committed yet\n", "未提交的改动也在"
    src, dst = _inodes(base), _inodes(copy)
    objects = [p for p in src if p.startswith(".git/objects/")]
    assert objects and all(dst[p] == src[p] for p in objects), "对象文件是硬链接"
    others = [p for p in src if not p.startswith(".git/objects/")]
    assert all(dst[p] != src[p] for p in others), "工作树、index、refs 是各自的文件"
    linked = sum((base / p).stat().st_size for p in objects)
    copied_bytes = sum((base / p).stat().st_size for p in others)
    print(f"N-10 mount: linked {linked} bytes in {len(objects)} object files, copied {copied_bytes} bytes")
    # 隔离：副本里提交不改源的分支与工作树；源里 gc 不坏副本。
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, workspace="w-copy", message="from the copy")
    assert git("rev-parse", "HEAD", cwd=copy) != git("rev-parse", "HEAD", cwd=base)
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, workspace="w-base"))["files"] == ["draft.md"], "源工作区不受影响"
    git("-c", "gc.pruneExpire=now", "gc", "--quiet", "--prune=now", cwd=base)
    git("fsck", "--no-progress", cwd=copy)
    assert git("log", "--format=%s", "-n", "2", cwd=copy).splitlines() == ["from the copy", "seed"]


async def test_archive_mode_references_the_source_snapshot(tmp_path: Path) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode="archive")
    await _base(s, url)
    before = len(await _blob_keys(s, "w-base"))
    envs_before = len(await _all_envs(s))
    copied = (await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy", from_workspace_id="w-base"))["workspace"]
    assert copied["branch"] == "main" and len(copied["head"]) == 40
    assert await _blob_keys(s, "w-copy") == [], "复制不搬块"
    assert len(await _all_envs(s)) == envs_before, "复制不起环境"
    print(f"N-10 archive: shared {before} blobs, wrote 0 for the copy")
    # 副本能用：内容与源一样（含未提交的改动），改了的文件才写进自己的块。
    files = (await s.run(Op.WORKSPACE_OPERATION_DIFF, workspace="w-copy"))["files"]
    assert files == ["draft.md"]
    await s.tool("repo_edit", {"path": "draft.md", "old": "not committed yet\n", "new": "changed in the copy\n"}, workspace="w-copy")
    await s.run(Op.WORKSPACE_OPERATION_ARCHIVE, workspace="w-copy")
    assert 1 <= len(await _blob_keys(s, "w-copy")) <= 3, "只写改了的文件（与 git 的 index）"
    # 源删除：副本还引用的块留着，副本照样能恢复。
    await s.run(Op.WORKSPACE_OPERATION_DELETE, workspace="w-base")
    assert await _blob_keys(s, "w-base"), "副本还引用的块留着"
    read = await s.run(Op.WORKSPACE_OPERATION_READ_FILE, workspace="w-copy", path="README.md")
    assert read["content"] == "# refund service\n"
    # 副本也删了：没人引用的块清掉。
    await s.run(Op.WORKSPACE_OPERATION_DELETE, workspace="w-copy")
    assert await _blob_keys(s, "w-base") == [] and await _blob_keys(s, "w-copy") == []
    assert before > 0


async def _blob_keys(s: Setup, workspace: str) -> list[str]:
    from agentruntime_worker.workspace.archive import _keys

    return sorted(await _keys(s.archiver._bucket("t1"), f"workspaces/{workspace}/blobs/"))  # noqa: SLF001


async def _all_envs(s: Setup) -> list[object]:
    async with s.ledger.bind("t1").transaction(read_only=True) as tx:
        return list(await tx.query("SELECT env_ref FROM sandbox_envs"))
