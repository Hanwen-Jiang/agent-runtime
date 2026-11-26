"""Reconstruct the first selection and publish the unused 33 import targets."""
import ast
import json
from cohort import HERE, NAVIGATION, REV, checksum, inventory, read_git


def remaining():
    sources, tests = inventory()
    defs = {}
    for mod, file in sources.items():
        for node in ast.parse(read_git(file)).body:
            if isinstance(node, (ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                defs[mod,node.name] = (file,node.lineno)
    pool = []
    for f in tests:
        tree = ast.parse(read_git(f))
        imports = {a.asname or a.name:(imp.module,a.name) for imp in tree.body
                   if isinstance(imp,ast.ImportFrom) and not imp.level for a in imp.names}
        for t in tree.body:
            if not isinstance(t,(ast.FunctionDef,ast.AsyncFunctionDef)) or not t.name.startswith("test_"):
                continue
            for call in ast.walk(t):
                if not isinstance(call,ast.Call) or not isinstance(call.func,ast.Name):
                    continue
                target = imports.get(call.func.id)
                if target in defs:
                    file,line = defs[target]
                    pool.append(dict(test_file=f,test=t.name,use_line=call.lineno,module=target[0],
                                     symbol=target[1],gold_file=file,gold_line=line))
    used = json.loads((HERE/"cohort.json").read_text())["questions"]
    test_keys = {(q["test_file"],q["test"]) for q in used}
    paths = {q["gold_file"] for q in used}
    symbols = {q["symbol"] for q in used}
    take = []
    for q in sorted(pool,key=lambda r:checksum("code-nav-v1/"+json.dumps(r,sort_keys=True))):
        key = (q["test_file"],q["test"])
        if key in test_keys or q["gold_file"] in paths or q["symbol"] in symbols:
            continue
        paths.add(q["gold_file"]);test_keys.add(key);symbols.add(q["symbol"])
        i = len(take)
        text = NAVIGATION[i % len(NAVIGATION)].format(symbol=q["symbol"],context=q["test"].removeprefix("test_").replace("_"," "))
        take.append(dict(id=f"res-{i+1:02d}",split="reserve",question=text,**q))
    assert len(take)==33
    return dict(revision=REV, questions=take)


if __name__=="__main__":
    print(json.dumps(remaining(),ensure_ascii=False,indent=2))
