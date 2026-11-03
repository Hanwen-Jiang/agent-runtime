"""BFCL v4 的单轮函数调用（docs/plan/capability-matrix-evals.md 第 3 节，role act）：按 BFCL 的 AST 规则判对错，不要裁判。

数据：Gorilla 仓库 `berkeley-function-call-leaderboard/bfcl_eval/data` 在钉死的提交上的 `BFCL_v4_<类别>.json` 与
`possible_answer/` 里的标准答案（Apache-2.0），逐个文件核对 sha256，缓存在 `<AR_EVAL_DATA_DIR>/bfcl-v4/<提交>/`。

默认类别 simple（simple_python）、multiple、parallel、parallel_multiple；另可选 live_simple、live_multiple、live_parallel、
live_parallel_multiple（用户贡献的真实函数）与 irrelevance、live_irrelevance（不该调用任何函数，调了就错）。
多轮（multi_turn_*）要执行 BFCL 自带的模拟 API 类（文件系统、交易等），没做，见实现记录。

怎么问（同 BFCL 的 FC 模式）：题目的消息原样给模型，函数按 BFCL 的 `convert_to_tool` 转成 JSON Schema 作为工具
（dict → object、float → number 并在说明后加 " This is a float type value."、名字里的 "." 换成 "_"，说明末尾加
" Note that the provided function is in Python 3 syntax."），模型的工具调用就是答案。

怎么判（移植自 `bfcl_eval/eval_checker/ast_eval/ast_checker.py` 的 Python 部分，Apache-2.0）：simple 要正好一个调用；
multiple 同 simple（候选函数多个）；parallel 与 parallel_multiple 不论顺序一一对上。参数：必填的要有、多出的不行、
字符串去掉空格与 `,./-_*^` 后不分大小写地比、列表与字典逐项比、可选参数可以不给（标准答案里有 ""）。

和原版的一处差别：Model Gateway 的工具参数是 protobuf Struct，数一律是浮点数，分不出模型写的是 `10` 还是 `10.0`。
这里按函数声明的类型还原：声明为 integer（含数组里的 integer）且值是整数的转成 int，其余保持浮点数。
所以模型给整数参数写 `10.0` 时原版判类型错、这里判对；别的规则不变。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "bfcl-v4"
ROLE = "act"
REPO = "ShishirPatil/gorilla"
COMMIT = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"  # 2026-03-23，main
RAW = "https://raw.githubusercontent.com/{repo}/{commit}/berkeley-function-call-leaderboard/bfcl_eval/data/{path}"
DEFAULT_CATEGORIES = ("simple", "multiple", "parallel", "parallel_multiple")
# 类别 → (数据文件, 题目的 sha256, 标准答案的 sha256；irrelevance 没有标准答案)
FILES: dict[str, tuple[str, str, str | None]] = {
    "simple": ("simple_python", "82dd63ba502eb2520c6b5d1d9a5c4b590e03ff261565175561f6228a367d1991", "90cd5bc653690ee8e459b5b3f3fc9458606f7f3fcbf795bb51b7dc581f8c86dc"),
    "multiple": ("multiple", "aef168155ebd74b7ac2401198b201343bc7d16d7a3d7e0d4e6d8ee82c6969b2a", "244e00ce9395df948bcafc7bee64e8f9c87ef70887587d83cae45b13699f3047"),
    "parallel": ("parallel", "19f51a82eff42e5d62541aa500115a056eb78f437c2ba1f10415fd7c8e5dda84", "8a6aa19c1adddc6a5a2f7e40f9dbf30cc7e95815e7b830c90589ab318229e0f0"),
    "parallel_multiple": ("parallel_multiple", "8863ea8433239f55c5f016154cf0830853c89f693c6ea270396a2fa121960579", "5ebf24f458c1f16300c05505d83d6f0a1b68b79be273a033febd0d4f840507e3"),
    "live_simple": ("live_simple", "1af2ac87dca47556db7b7e37e51e28b459a38b594e3c7b3c792b4903598ca0c4", "fec9cfa9744a936f9126981e85a2023da1e63e273eafebc81923a1162fad70ce"),
    "live_multiple": ("live_multiple", "fd8ccfad4d911420d0e3341dbe2fff77d1d341da934248b9bb2bda24ab3a10c8", "97e90d59c5bd76c55a2920ce93e5566e9046307d3f558578f085f9d3a56c3084"),
    "live_parallel": ("live_parallel", "6c26e9fdc3350cf596e6d1ea9c179cbff834761bccf562f4141ed29a839ca421", "8a9f189ff0e832ebbbbdade1fd95a7dbcc67406e9177df3f0aad76f59ab00350"),
    "live_parallel_multiple": ("live_parallel_multiple", "21d4b9319c1faac431e22757b367ea28917fe467364c3a4b17f16ec06d4f6e79", "f5b5f360556c5feb51db46fb9f56ee4b304f4b45b161599bbb14161c98a2873f"),
    "irrelevance": ("irrelevance", "2b6ed4c2e992cdcf5f1678a701851f944bef7550ee026ed1ddb89efed5be01a6", None),
    "live_irrelevance": ("live_irrelevance", "6559fda2beaceb609a2cd2e504c65b4a56cb448e1ef88fddfd199e163d163349", None),
}

# bfcl_eval/constants/type_mappings.py 的 GORILLA_TO_OPENAPI
GORILLA_TO_OPENAPI = {
    "integer": "integer", "number": "number", "float": "number", "string": "string", "boolean": "boolean", "bool": "boolean",
    "array": "array", "list": "array", "dict": "object", "object": "object", "tuple": "array", "any": "string", "byte": "integer",
    "short": "integer", "long": "integer", "double": "number", "char": "string", "ArrayList": "array", "Array": "array",
    "HashMap": "object", "Hashtable": "object", "Queue": "array", "Stack": "array", "Any": "string", "String": "string", "Bigint": "integer",
}
PYTHON_TYPE_MAPPING = {"string": str, "integer": int, "float": float, "boolean": bool, "array": list, "tuple": list, "dict": dict, "any": str}
PYTHON_NESTED_TYPE_CHECK_LIST = ("array", "tuple")
PYTHON_HINT = " Note that the provided function is in Python 3 syntax."


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]


def load(categories: Sequence[str], root: Path | None = None, *, commit: str = COMMIT) -> dict[str, dict[str, Any]]:
    """题目 id → {category, question, function, ground_truth}；root 为空时从缓存读（缺的先下载并核对哈希）。"""
    out: dict[str, dict[str, Any]] = {}
    for cat in categories:
        if cat not in FILES:
            raise ValueError(f"BFCL 没有类别 {cat!r}：只有 {'、'.join(FILES)}")
        stem, q_sha, a_sha = FILES[cat]
        if root is None:
            base = ds.data_dir() / NAME / commit
            questions = ds.download(RAW.format(repo=REPO, commit=commit, path=f"BFCL_v4_{stem}.json"), base / f"BFCL_v4_{stem}.json", sha256=q_sha)
            answers = (ds.download(RAW.format(repo=REPO, commit=commit, path=f"possible_answer/BFCL_v4_{stem}.json"),
                                   base / "possible_answer" / f"BFCL_v4_{stem}.json", sha256=a_sha) if a_sha else None)
        else:
            questions = Path(root) / f"BFCL_v4_{stem}.json"
            answers = Path(root) / "possible_answer" / f"BFCL_v4_{stem}.json" if a_sha else None
        truth = {r["id"]: r["ground_truth"] for r in _read_json_lines(answers)} if answers else {}
        for row in _read_json_lines(questions):
            if answers and row["id"] not in truth:
                raise ValueError(f"{row['id']} 没有标准答案")
            out[row["id"]] = {"category": cat, "question": row["question"], "function": row["function"], "ground_truth": truth.get(row["id"])}
    return out


# --- 工具定义（bfcl_eval/model_handler/utils.py 的 convert_to_tool 与 _cast_to_openai_type） ---


def _cast(properties: dict[str, Any]) -> dict[str, Any]:
    for key, spec in properties.items():
        if "type" not in spec:
            spec["type"] = "string"
        else:
            var_type = spec["type"]
            if var_type == "float":
                spec["format"] = "float"
                spec["description"] = spec.get("description", "") + " This is a float type value."
            spec["type"] = GORILLA_TO_OPENAPI.get(var_type, "string")
        if spec["type"] in ("array", "object"):
            if "properties" in spec:
                spec["properties"] = _cast(spec["properties"])
            elif "items" in spec:
                items = spec["items"]
                items["type"] = GORILLA_TO_OPENAPI[items["type"]]
                if items["type"] == "array" and "items" in items:
                    items["items"]["type"] = GORILLA_TO_OPENAPI[items["items"]["type"]]
                elif items["type"] == "object" and "properties" in items:
                    items["properties"] = _cast(items["properties"])
    return properties


def tool_name(name: str) -> str:
    return re.sub(r"\.", "_", name)


def to_tool(function: Mapping[str, Any]) -> dict[str, Any]:
    f = copy.deepcopy(dict(function))
    params = f.get("parameters") or {"type": "dict", "properties": {}}
    params["type"] = "object"
    params["properties"] = _cast(params.get("properties") or {})
    return {"name": tool_name(f["name"]), "description": f.get("description", "") + PYTHON_HINT, "parameters": params}


def restore_ints(value: Any, spec: Mapping[str, Any] | None) -> Any:
    """按 BFCL 的参数类型把 Struct 里的整数值还原成 int（见模块说明）。"""
    kind = (spec or {}).get("type")
    if kind == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if kind in ("array", "tuple") and isinstance(value, list):
        return [restore_ints(v, (spec or {}).get("items")) for v in value]
    if kind == "dict" and isinstance(value, dict) and "properties" in (spec or {}):
        props = spec["properties"]  # type: ignore[index]
        return {k: restore_ints(v, props.get(k)) for k, v in value.items()}
    return value


# --- AST 判分（移植自 ast_checker.py，只留 Python） ---


def _ok() -> dict[str, Any]:
    return {"valid": True, "error": []}


def _bad(error: str, kind: str) -> dict[str, Any]:
    return {"valid": False, "error": [error], "error_type": kind}


def find_description(functions: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    return next((f for f in functions if f["name"] == name), None)


def _answer_type(possible: list[Any]) -> type | None:
    for answer in possible:
        if answer != "":
            return type(answer)
    return None


def type_checker(param: str, value: Any, possible: list[Any], expected_desc: str, expected: type, nested: type | None) -> dict[str, Any]:
    result = {"valid": True, "error": [], "is_variable": False, "error_type": "type_error:simple"}
    is_variable = False
    possible_type = _answer_type(possible)
    if possible_type is not None and possible_type != expected:
        is_variable = True
    if type(value) == expected:  # noqa: E721 — 同原版，bool 不算 int
        if nested is None:
            result["is_variable"] = is_variable
            return result
        for item in possible:
            flag = True
            if type(item) == list:  # noqa: E721
                for v in value:
                    if not type_checker(param, v, item, str(nested), nested, None)["valid"]:
                        flag = False
                        break
            if flag:
                return {"valid": True, "error": [], "is_variable": is_variable}
        result["valid"] = False
        result["error"] = [f"Nested type checking failed for parameter {param!r}. Expected outer type {expected_desc} with inner type {nested}. Parameter value: {value!r}."]
        result["error_type"] = "type_error:nested"
    possible_type = _answer_type(possible)
    if possible_type is not None and type(value) == possible_type:  # noqa: E721
        result["is_variable"] = True
        return result
    result["valid"] = False
    result["error"].append(f"Incorrect type for parameter {param!r}. Expected type {expected_desc}, got {type(value).__name__}. Parameter value: {value!r}.")
    result["error_type"] = "type_error:simple"
    return result


def standardize_string(text: str) -> str:
    return re.sub(r"[ \,\.\/\-\_\*\^]", "", text).lower().replace("'", '"')


def string_checker(param: str, value: str, possible: list[Any]) -> dict[str, Any]:
    if standardize_string(value) not in [standardize_string(p) for p in possible if type(p) == str]:  # noqa: E721
        return _bad(f"Invalid value for parameter {param!r}: {value!r}. Expected one of {possible}. Case insensitive.", "value_error:string")
    return _ok()


def list_checker(param: str, value: list[Any], possible: list[Any]) -> dict[str, Any]:
    std = [standardize_string(v) if type(v) == str else v for v in value]  # noqa: E721
    std_possible = [[standardize_string(x) if type(x) == str else x for x in p] for p in possible]  # noqa: E721
    if std not in std_possible:
        return _bad(f"Invalid value for parameter {param!r}: {value!r}. Expected one of {possible}.", "value_error:list/tuple")
    return _ok()


def dict_checker(param: str, value: dict[str, Any], possible_answers: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"valid": False, "error": [], "error_type": "dict_checker:unclear"}
    for possible in possible_answers:
        if possible == "":
            continue
        result = {"valid": False, "error": [], "error_type": "dict_checker:unclear"}
        flag = True
        for key, v in value.items():
            if key not in possible:
                result = _bad(f"Unexpected dict key parameter: '{key}'.", "value_error:dict_key")
                flag = False
                break
            std = standardize_string(v) if type(v) == str else v  # noqa: E721
            std_possible = [standardize_string(x) if type(x) == str else x for x in possible[key]]  # noqa: E721
            if std not in std_possible:
                result = _bad(f"Invalid value for parameter {key!r}: {v!r}. Expected one of {std_possible}.", "value_error:dict_value")
                flag = False
                break
        for key, v in possible.items():
            if key not in value and "" not in v:
                result = _bad(f"Missing dict key parameter: '{key}'.", "value_error:dict_key")
                flag = False
                break
        if flag:
            return _ok()
    return result


def list_dict_checker(param: str, value: list[Any], possible_answers: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"valid": False, "error": [], "error_type": "list_dict_checker:unclear"}
    for possible in possible_answers:
        if len(value) != len(possible):
            result = _bad("Wrong number of dictionaries in the list.", "value_error:list_dict_count")
            continue
        flag = True
        for i, item in enumerate(value):
            result = dict_checker(param, item, [possible[i]])
            if not result["valid"]:
                flag = False
                break
        if flag:
            return _ok()
    return result


def simple_function_checker(function: Mapping[str, Any], output: Mapping[str, Any], possible_answer: Mapping[str, Any]) -> dict[str, Any]:
    answer = list(possible_answer.values())[0]
    name = tool_name(function["name"])
    details = function["parameters"]["properties"]
    required = function["parameters"].get("required", [])
    if name not in output:
        return _bad(f"Function name {name!r} not found in model output.", "simple_function_checker:wrong_func_name")
    params = output[name]
    for p in required:
        if p not in params:
            return _bad(f"Missing required parameter: {p!r}.", "simple_function_checker:missing_required")
    for p, v in params.items():
        if p not in details or p not in answer:
            return _bad(f"Unexpected parameter: {p!r}.", "simple_function_checker:unexpected_param")
        desc = details[p]["type"]
        expected = PYTHON_TYPE_MAPPING[desc]
        nested = PYTHON_TYPE_MAPPING[details[p]["items"]["type"]] if desc in PYTHON_NESTED_TYPE_CHECK_LIST else None
        if desc == "tuple" and type(v) == tuple:  # noqa: E721
            v = list(v)
        if desc == "float" and type(v) == int:  # noqa: E721
            v = float(v)
        checked = type_checker(p, v, answer[p], desc, expected, nested)
        if not checked["valid"]:
            return checked
        if not checked["is_variable"]:
            if expected is dict:
                res = dict_checker(p, v, answer[p])
            elif expected is list and nested is dict:
                res = list_dict_checker(p, v, answer[p])
            elif expected is str:
                res = string_checker(p, v, answer[p])
            elif expected is list:
                res = list_checker(p, v, answer[p])
            else:
                res = None
            if res is not None:
                if not res["valid"]:
                    return res
                continue
        if v not in answer[p]:
            return _bad(f"Invalid value for parameter {p!r}: {v!r}. Expected one of {answer[p]}.", "value_error:others")
    for p in answer:
        if p not in params and "" not in answer[p]:
            return _bad(f"Optional parameter {p!r} not provided and not marked as optional.", "simple_function_checker:missing_optional")
    return _ok()


def parallel_no_order(functions: Sequence[Mapping[str, Any]], output: list[Mapping[str, Any]], answers: list[Mapping[str, Any]]) -> dict[str, Any]:
    if len(output) != len(answers):
        return _bad("Wrong number of functions.", "parallel_function_checker_no_order:wrong_count")
    matched: list[int] = []
    for i, answer in enumerate(answers):
        function = find_description(functions, list(answer.keys())[0])
        result = _bad("no candidate", "parallel_function_checker_no_order:cannot_find_match")
        for index, item in enumerate(output):
            if index in matched:
                continue
            result = simple_function_checker(function, item, answer) if function else _bad("no description", "unknown_function")
            if result["valid"]:
                matched.append(index)
                break
        if not result["valid"]:
            return _bad(f"Could not find a matching function among index {[j for j in range(len(output)) if j not in matched]} of model output "
                        f"for index {i} of possible answers.", "parallel_function_checker_no_order:cannot_find_match")
    return _ok()


def ast_check(category: str, functions: Sequence[Mapping[str, Any]], output: list[Mapping[str, Any]], answers: list[Mapping[str, Any]]) -> dict[str, Any]:
    """BFCL 的 ast_checker：函数名在两边都按 tool_name 换过 "."。"""
    functions = [{**f, "name": tool_name(f["name"])} for f in functions]
    answers = [{tool_name(k): v for k, v in a.items()} for a in answers]
    if "parallel" in category:
        return parallel_no_order(functions, output, answers)
    if len(output) != len(answers) or len(output) != 1:
        kind = "multiple_function_checker:wrong_count" if "multiple" in category else "simple_function_checker:wrong_count"
        return _bad("Wrong number of functions.", kind)
    function = find_description(functions, list(answers[0].keys())[0])
    if function is None:
        return _bad("Function description not found.", "unknown_function")
    return simple_function_checker(function, output[0], answers[0])


class BfclEval:
    name = NAME
    role = ROLE
    est_tokens = (600, 100)  # 矩阵估算花费用：每题被测一侧的输入、输出 token

    def __init__(self, *, categories: Sequence[str] = DEFAULT_CATEGORIES, root: Path | None = None, max_output_tokens: int = 4096) -> None:
        self.categories = tuple(categories)
        self.root = root
        self.max_output_tokens = max_output_tokens
        tag = "" if self.categories == DEFAULT_CATEGORIES else ":" + "+".join(self.categories)
        self.version = f"gorilla@{COMMIT[:7]}{tag}"
        self._rows: dict[str, dict[str, Any]] | None = None

    def rows(self) -> dict[str, dict[str, Any]]:
        if self._rows is None:
            self._rows = load(self.categories, self.root)
        return self._rows

    def items(self, n: int, seed: int) -> list[Item]:
        rows = self.rows()
        return [Item(i, rows[i]) for i in ds.select(sorted(rows), n, seed, group=lambda i: rows[i]["category"])]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        meter = ds.Meter(gateway, item_id=item.id, eval_name=self.name)
        p = item.payload
        record: dict[str, Any] = {"category": p["category"], "candidate": candidate}
        try:
            turns = p["question"]
            if len(turns) != 1:
                raise ValueError("只做单轮的题")
            messages = [ds.chat(m["role"], m["content"]) for m in turns[0]]
            tools = [to_tool(f) for f in p["function"]]
            response = await meter.generate(role=ROLE, node="bfcl", messages=messages, max_output_tokens=self.max_output_tokens,
                                            tools=[ds.tool_spec(t["name"], t["description"], t["parameters"]) for t in tools])
            specs = {tool_name(f["name"]): (f.get("parameters") or {}).get("properties") or {} for f in p["function"]}
            output = [{c.name: {k: restore_ints(v, specs.get(c.name, {}).get(k)) for k, v in ds.arguments(c).items()}} for c in response.tool_calls]
            record.update({"text": response.text, "output": output})
            if "irrelevance" in p["category"]:
                check = _ok() if not output else _bad("Valid syntax. Successfully decode AST when it should not.", "irrelevance_error:decoder_success")
            else:
                check = ast_check(p["category"], p["function"], output, p["ground_truth"])
            record["check"] = check
            return meter.result(1.0 if check["valid"] else 0.0, record)
        except Exception as exc:
            return meter.error(exc, record)


def make(**options: Any) -> BfclEval:
    return BfclEval(**options)


__all__ = ["BfclEval", "ast_check", "load", "make", "restore_ints", "to_tool"]
