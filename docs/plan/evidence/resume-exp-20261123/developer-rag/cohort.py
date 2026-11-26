"""Build definition-navigation labels from real direct imports and calls in tests."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / ".git").exists())
REV = "5af1d890195dda4524634b33fd81beb414467079"
SCOPE = ("libs/py-core/", "services/py-worker/", "services/py-learning/", "services/py-memory/")
NAVIGATION = [
    "维护 {context} 时，`{symbol}` 的定义在哪个文件、哪一行？",
    "Where is `{symbol}` defined? It is used in {context}.",
    "定位 `{symbol}` 的定义，我在看 {context}。",
    "Find the definition of `{symbol}` used by {context}.",
]


def checksum(s):
    return hashlib.sha256(s.encode()).hexdigest()


def read_git(path):
    return subprocess.check_output(["git", "show", f"{REV}:{path}"], cwd=REPO)


def inventory():
    raw = subprocess.check_output(["git", "ls-tree", "-lr", "-z", REV], cwd=REPO)
    sources, tests = {}, []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.decode().split("\t", 1)
        if not path.startswith(SCOPE) or not path.endswith(".py"):
            continue
        if "/src/" in path and int(meta.split()[-1]) <= 32_000:
            module = path.partition("/src/")[2][:-3].replace("/", ".").removesuffix(".__init__")
            sources[module] = path
        if "/tests/test_" in path:
            tests.append(path)
    return sources, tests


def build():
    sources, tests = inventory()
    symbols = {}
    for module, path in sources.items():
        for node in ast.parse(read_git(path)).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols[module, node.name] = (path, node.lineno)
    cases = []
    for path in tests:
        tree = ast.parse(read_git(path))
        imports = {a.asname or a.name: (node.module, a.name) for node in tree.body
                   if isinstance(node, ast.ImportFrom) and not node.level for a in node.names}
        for test in tree.body:
            if not isinstance(test, (ast.FunctionDef, ast.AsyncFunctionDef)) or not test.name.startswith("test_"):
                continue
            for call in ast.walk(test):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                key = imports.get(call.func.id)
                if key not in symbols:
                    continue
                target, line = symbols[key]
                cases.append(dict(test_file=path, test=test.name, use_line=call.lineno,
                                  module=key[0], symbol=key[1], gold_file=target, gold_line=line))
    selection = []
    used_files, used_tests, used_symbols = set(), set(), set()
    for c in sorted(cases, key=lambda r: checksum("code-nav-v1/" + json.dumps(r, sort_keys=True))):
        test_key = (c['test_file'], c['test'])
        if c['gold_file'] in used_files or test_key in used_tests or c['symbol'] in used_symbols:
            continue
        used_files.add(c['gold_file']); used_tests.add(test_key); used_symbols.add(c['symbol'])
        selection.append(c)
    n_dev = min(24, len(selection) // 3)
    n_val = min(48, len(selection) - n_dev)
    rows = []
    for split, subset in [('development', selection[:n_dev]), ('validation', selection[n_dev:n_dev+n_val])]:
        for i, case in enumerate(subset):
            ctx = case['test'].removeprefix('test_').replace('_', ' ')
            question = NAVIGATION[i % len(NAVIGATION)].format(symbol=case['symbol'], context=ctx)
            rows.append(dict(id=f"{split[:3]}-{i+1:02d}", split=split, question=question, **case))
    return dict(revision=REV, source_paths=sorted(sources.values()), eligible_files=len(sources),
                distinct_eligible_after_deduplication=len(selection), questions=rows)


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
