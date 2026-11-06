"""D-25 through a real Sandbox Service, for both workspace storage modes."""
from pathlib import Path
import pytest
from .test_workspace_operations import Setup, Op, make_remote, git

@pytest.mark.parametrize("mode", ["mount", "archive"])
async def test_multirepo_lifecycle_and_isolation(tmp_path: Path, mode: str) -> None:
    url, bare = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    created = await s.run(Op.WORKSPACE_OPERATION_CREATE, layout="multi")
    assert created["workspace"]["layout"] == "WORKSPACE_LAYOUT_MULTI"
    assert (await s.error(Op.WORKSPACE_OPERATION_DIFF)).code == "repo_required"
    a = await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO, repo={"name":"api", "path":"api", "url":url, "access":"write"})
    b = await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO, repo={"name":"web", "path":"web", "url":url})
    assert a["repo"]["primary"] and b["repo"]["access"] == "REPO_ACCESS_READ"
    assert [r["name"] for r in (await s.run(Op.WORKSPACE_OPERATION_LIST_REPOS))["repos"]] == ["api","web"]
    assert (await s.error(Op.WORKSPACE_OPERATION_COMMIT, repo="web", message="denied")).code == "repo_read_only"
    assert (await s.error(Op.WORKSPACE_OPERATION_PUSH, repo="web", branch="agent/x")).code == "repo_read_only"
    assert (await s.error(Op.WORKSPACE_OPERATION_LOG, repo="missing")).code == "repo_not_found"
    await s.tool("repo_edit", {"path":"api/extra.txt", "new":"isolated\n"})
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, repo="api"))["files"] == ["extra.txt"]
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, repo="web"))["files"] == []
    assert (await s.error(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api")).code == "repo_dirty"
    committed = await s.run(Op.WORKSPACE_OPERATION_COMMIT, repo="api", message="api change")
    assert committed["committed"]
    assert (await s.error(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api")).code == "repo_dirty"
    pushed = await s.run(Op.WORKSPACE_OPERATION_PUSH, repo="api", branch="agent/multi")
    assert git("rev-parse","agent/multi",cwd=bare) == pushed["sha"]
    await s.tool("repo_edit", {"path":"api/unpushed.txt", "new":"pending\n"})
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, repo="api", message="not pushed")
    await s.run(Op.WORKSPACE_OPERATION_ARCHIVE)
    copy = await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="copy", layout="multi", from_workspace_id="w1")
    assert [r["name"] for r in copy["workspace"]["repos"]] == ["api","web"]
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, workspace="copy", path="api/extra.txt"))["content"] == "isolated\n"
    gone = await s.run(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api", discard_changes=True)
    assert gone["discarded_commits"] == 1
    assert gone["workspace"]["repos"][0]["name"] == "web" and gone["workspace"]["repos"][0]["primary"]
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE,path="web/README.md"))["content"]
    assert (await s.error(Op.WORKSPACE_OPERATION_LIST_REPOS,tenant="t2")).code == "workspace_not_found"
    await s.run(Op.WORKSPACE_OPERATION_DELETE)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, layout="multi")
    assert (await s.run(Op.WORKSPACE_OPERATION_LIST_REPOS))["repos"] == []

async def test_multirepo_boundaries_and_partial_success(tmp_path: Path) -> None:
    url,_ = make_remote(tmp_path)
    s=Setup(tmp_path,mode="mount",policy={"workspace":{"max_repos":2}})
    await s.run(Op.WORKSPACE_OPERATION_CREATE)
    assert (await s.error(Op.WORKSPACE_OPERATION_LIST_REPOS)).code == "workspace_not_multi"
    assert (await s.error(Op.WORKSPACE_OPERATION_DIFF,repo="api")).code == "repo_not_applicable"
    await s.run(Op.WORKSPACE_OPERATION_CREATE,workspace="m",layout="multi")
    assert (await s.error(Op.WORKSPACE_OPERATION_CREATE,workspace="bad",layout="multi",from_workspace_id="w1")).code == "layout_mismatch"
    for path in ["../x","/x",".git","a/b","a\\b"]:
        assert (await s.error(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"bad","path":path,"url":url})).code == "invalid_repo_path"
    await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"a","path":"Api","url":url})
    assert (await s.error(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"b","path":"api","url":url})).code == "repo_path_conflict"
    assert (await s.error(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"b","path":"b","url":url,"primary":True})).code == "primary_exists"
    await s.error(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"broken","path":"broken","url":url+"-missing"})
    assert len((await s.run(Op.WORKSPACE_OPERATION_LIST_REPOS,workspace="m"))["repos"]) == 1
    await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"b","path":"b","url":url})
    assert (await s.error(Op.WORKSPACE_OPERATION_ATTACH_REPO,workspace="m",repo={"name":"c","path":"c","url":url})).code == "max_repos"


@pytest.mark.parametrize("mode", ["mount", "archive"])
async def test_detach_preserves_repository_when_commit_inventory_is_broken(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, layout="multi")
    await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO, repo={"name": "api", "path": "api", "url": url, "access": "write"})
    await s.tool("repo_edit", {"path": "api/local.txt", "new": "only local copy\n"})
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, repo="api", message="unpublished")
    await s.tool("shell_run", {"argv": ["python", "-c", "from pathlib import Path; Path('api/.git/refs/remotes/origin/broken').write_text('f'*40+'\\n')"]})
    error = await s.error(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api")
    assert error.code == "git_not_found"
    assert [r["name"] for r in (await s.run(Op.WORKSPACE_OPERATION_LIST_REPOS))["repos"]] == ["api"]
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="api/local.txt"))["content"] == "only local copy\n"


@pytest.mark.parametrize("mode", ["mount", "archive"])
async def test_detach_checks_unpublished_commits_on_other_branches(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, layout="multi")
    await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO, repo={"name": "api", "path": "api", "url": url, "access": "write"})
    await s.tool("shell_run", {"argv": ["git", "-C", "api", "checkout", "-b", "local-work"]})
    await s.tool("repo_edit", {"path": "api/local.txt", "new": "unpublished branch\n"})
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, repo="api", message="unpublished")
    await s.tool("shell_run", {"argv": ["git", "-C", "api", "checkout", "main"]})
    assert (await s.error(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api")).code == "repo_dirty"
    removed = await s.run(Op.WORKSPACE_OPERATION_DETACH_REPO, repo="api", discard_changes=True)
    assert removed["discarded_commits"] == 1


@pytest.mark.parametrize("mode", ["mount", "archive"])
async def test_repository_git_metadata_cannot_redirect_operations_to_another_repo(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, layout="multi")
    for name in ("a", "b"):
        await s.run(Op.WORKSPACE_OPERATION_ATTACH_REPO, repo={"name": name, "path": name, "url": url, "access": "write" if name == "a" else "read"})
    await s.tool("repo_edit", {"path": "b/private.txt", "new": "keep other repository\n"})
    await s.tool("shell_run", {"argv": ["git", "-C", "a", "config", "core.worktree", "../../b"]})
    await s.run(Op.WORKSPACE_OPERATION_DISCARD, repo="a")
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="b/private.txt"))["content"] == "keep other repository\n"
    await s.tool("shell_run", {"argv": ["python", "-c", "import os,shutil; shutil.rmtree('a/.git'); os.symlink('../b/.git','a/.git')"]})
    assert (await s.error(Op.WORKSPACE_OPERATION_COMMIT, repo="a", message="must not touch b")).code == "invalid_repo_path"
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="b/private.txt"))["content"] == "keep other repository\n"
