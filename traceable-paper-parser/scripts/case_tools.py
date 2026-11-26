#!/usr/bin/env python3
"""Inspect case contracts, search evidence, validate results and submission resources."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

sys.dont_write_bytecode = True


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def output(value):
    print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))


def contract_schema(case):
    contract = case.get("output_contract")
    if contract is None and isinstance(case.get("task"), dict):
        contract = case["task"].get("output_contract")
    if not isinstance(contract, (dict, list)):
        return None
    if isinstance(contract, list):
        fields = [v if isinstance(v, str) else v.get("name") if isinstance(v, dict) else None for v in contract]
        return {"type":"object", "required": fields} if all(isinstance(k,str) for k in fields) else None
    if "result.json" in contract and isinstance(contract["result.json"], dict):
        contract = contract["result.json"]
    if isinstance(contract.get("schema"), dict):
        return contract["schema"]
    if any(key in contract for key in ("type","properties","required","allOf","anyOf","oneOf","$ref")):
        return contract
    if "fields" in contract:
        fields = contract["fields"]
        if isinstance(fields, list):
            names = [f if isinstance(f,str) else f.get("name") if isinstance(f,dict) else None for f in fields]
            return {"type":"object", "required":names} if all(isinstance(k,str) for k in names) else None
        if isinstance(fields, dict):
            contract = fields
        else:
            return None
    props = {}
    for key, value in contract.items():
        if isinstance(value, dict):
            props[key] = value
        elif value in ("string","number","integer","object","array","boolean","null"):
            props[key] = {"type": value}
        else:
            props[key] = {}
    return {"type":"object", "required":list(contract), "properties":props}


def schema_errors(value, schema, root=None, location="$", depth=0):
    if depth > 40:
        return [f"{location}: schema nesting/ref depth exceeded"]
    if schema is True:
        return []
    if schema is False:
        return [f"{location}: disallowed by schema"]
    if not isinstance(schema, dict):
        return [f"{location}: unrecognized schema node"]
    root = schema if root is None else root
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref,str) or not ref.startswith("#/"):
            return [f"{location}: only local schema references are supported"]
        target = root
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1","/").replace("~0","~")]
        except (KeyError,TypeError):
            return [f"{location}: unresolved local schema reference"]
        return schema_errors(value,target,root,location,depth+1)
    errors = []
    checks = {"object":lambda v:isinstance(v,dict), "array":lambda v:isinstance(v,list),
              "string":lambda v:isinstance(v,str), "number":lambda v:isinstance(v,(int,float)) and not isinstance(v,bool),
              "integer":lambda v:isinstance(v,int) and not isinstance(v,bool), "boolean":lambda v:isinstance(v,bool),
              "null":lambda v:v is None}
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"],list) else [schema["type"]]
        if not any(checks.get(t,lambda v:False)(value) for t in types):
            return [f"{location}: expected type {types}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{location}: value not in enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{location}: does not match const")
    for sub in schema.get("allOf",[]):
        errors += schema_errors(value,sub,root,location,depth+1)
    for key, minimum, maximum in (("anyOf",1,None),("oneOf",1,1)):
        if key in schema:
            successes = sum(not schema_errors(value,s,root,location,depth+1) for s in schema[key])
            if successes < minimum or maximum is not None and successes > maximum:
                errors.append(f"{location}: failed {key}")
    if "not" in schema and not schema_errors(value,schema["not"],root,location,depth+1):
        errors.append(f"{location}: prohibited by not")
    if isinstance(value,dict):
        for key in schema.get("required",[]):
            if key not in value:
                errors.append(f"{location}.{key}: required field missing")
        properties = schema.get("properties",{})
        for key,item in value.items():
            if key in properties:
                errors += schema_errors(item,properties[key],root,f"{location}.{key}",depth+1)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{location}.{key}: additional field disallowed")
            elif isinstance(schema.get("additionalProperties"),dict):
                errors += schema_errors(item,schema["additionalProperties"],root,f"{location}.{key}",depth+1)
    if isinstance(value,list):
        for key,comparison in (("minItems",lambda x,n:x<n),("maxItems",lambda x,n:x>n)):
            if key in schema and comparison(len(value),schema[key]):
                errors.append(f"{location}: violates {key}")
        if schema.get("uniqueItems") and len({json.dumps(v,sort_keys=True) for v in value}) != len(value):
            errors.append(f"{location}: duplicate array items")
        items = schema.get("items",{})
        if isinstance(items,(dict,bool)):
            for i,item in enumerate(value):
                errors += schema_errors(item,items,root,f"{location}[{i}]",depth+1)
    if isinstance(value,str):
        for key,comparison in (("minLength",lambda x,n:x<n),("maxLength",lambda x,n:x>n)):
            if key in schema and comparison(len(value),schema[key]):
                errors.append(f"{location}: violates {key}")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"],value):
                    errors.append(f"{location}: does not match pattern")
            except re.error:
                errors.append(f"{location}: pattern requires manual verification")
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if not math.isfinite(value):
            errors.append(f"{location}: non-finite number")
        for key,comparison in (("minimum",lambda x,n:x<n),("maximum",lambda x,n:x>n),
                               ("exclusiveMinimum",lambda x,n:x<=n),("exclusiveMaximum",lambda x,n:x>=n)):
            if key in schema and comparison(value,schema[key]):
                errors.append(f"{location}: violates {key}")
    return errors


def inspect_case(path):
    case = load(path)
    if not isinstance(case,dict):
        raise ValueError("case.json must have an object root")
    pdfs = []
    def walk(value, trail="$"):
        if isinstance(value,dict):
            for k,v in value.items():
                if not re.search(r"password|secret|token|api.?key",k,re.I):
                    walk(v,trail+"."+k)
        elif isinstance(value,list):
            for i,v in enumerate(value): walk(v,f"{trail}[{i}]")
        elif isinstance(value,str) and re.search(r"\.pdf(?:$|[?#])",value,re.I):
            pdfs.append({"field":trail,"value":value})
    walk(case)
    schema = contract_schema(case)
    output({"case_id":case.get("case_id"), "top_level_keys":list(case), "pdf_references":pdfs,
            "output_contract":case.get("output_contract",case.get("task",{}).get("output_contract") if isinstance(case.get("task"),dict) else None),
            "machine_checkable_contract":schema is not None,
            "instruction":"Read the actual task and resource manifest. Referenced PDF paths are not proof that files exist."})
    return 0


def search(directory, query, limit, kind):
    tokens = re.findall(r"[\w]+",unicodedata.normalize("NFKC",query).lower())
    if not tokens:
        raise ValueError("Query contains no searchable tokens")
    matches = []
    with (Path(directory)/"anchors.jsonl").open(encoding="utf-8") as f:
        for line in f:
            anchor = json.loads(line)
            atype = anchor.get("anchor_type","block")
            if kind != "all" and (atype == "line" if kind == "block" else atype != kind):
                continue
            text = anchor.get("text") or ""
            normalized = unicodedata.normalize("NFKC",text).lower()
            score = sum(normalized.count(token) for token in tokens)
            if score:
                matches.append((score,{k:anchor.get(k) for k in ("document_id","source_pdf","page_number","id","bbox","text","source_method")}))
    matches.sort(key=lambda pair:(-pair[0],pair[1]["page_number"],pair[1]["id"]))
    output({"query":query,"matches":[item for _,item in matches[:limit]],"match_count":len(matches)})
    return 0


def check_result(case_path, result_path):
    case, result = load(case_path), load(result_path)
    errors = []
    if not isinstance(case,dict) or not isinstance(result,dict):
        output({"valid":False,"errors":["Case and result must be objects"]})
        return 2
    if "case_id" not in case:
        errors.append("Original case_id not found; inspect case contract manually")
    elif result.get("case_id") != case["case_id"] or type(result.get("case_id")) is not type(case["case_id"]):
        errors.append("case_id differs from original input (including type)")
    schema = contract_schema(case)
    if schema is None:
        errors.append("output_contract is not machine-checkable; manual verification required")
    else:
        errors += schema_errors(result,schema)
    output({"valid":not errors,"errors":errors,"semantic_correctness_checked":False,
            "scope":"Common JSON Schema rules and exact case_id only; unsupported constraints require manual checks."})
    return 0 if not errors else 2


def audit(directory):
    directory = Path(directory)
    doc = load(directory/"document.json")
    errors = []
    pages = {p["page_number"]:p for p in doc["pages"]}
    ids = set()
    count = 0
    for text in (directory/"anchors.jsonl").read_text(encoding="utf-8").splitlines():
        anchor = json.loads(text)
        count += 1
        identifier = anchor["id"]
        if identifier in ids: errors.append(f"Duplicate anchor: {identifier}")
        ids.add(identifier)
        page = pages.get(anchor["page_number"])
        b = anchor.get("bbox")
        if page is None or not isinstance(b,list) or len(b)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in b):
            errors.append(f"Invalid page/bbox: {identifier}")
        elif b[0]>b[2] or b[1]>b[3] or b[0]<-5 or b[1]<-5 or b[2]>page["width"]+5 or b[3]>page["height"]+5:
            errors.append(f"BBox outside normalized page: {identifier}")
        if anchor.get("document_id") != doc["document_id"]:
            errors.append(f"Document identity mismatch: {identifier}")
    for page in pages.values():
        for block in page["blocks"]:
            if any(i not in ids for i in block["line_ids"]):
                errors.append(f"Unresolvable line references: {block['id']}")
        for table in page["tables"]:
            if not (directory/table["html_path"]).is_file():
                errors.append(f"Missing table HTML: {table['id']}")
    output({"valid":not errors,"anchor_count":count,"errors":errors})
    return 0 if not errors else 2


def check_package(directory):
    directory=Path(directory)
    errors=[]; files=[]; total=0
    for p in sorted(directory.rglob("*")):
        if p.is_symlink():
            errors.append(f"Symlink: {p.relative_to(directory)}")
        elif p.is_file():
            data=p.read_bytes(); total+=len(data)
            files.append({"path":p.relative_to(directory).as_posix(),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
            if len(data)>500_000: errors.append(f"File exceeds 500KB: {p.name}")
            try: data.decode("utf-8")
            except UnicodeDecodeError: errors.append(f"Not UTF-8 text: {p.name}")
    if len(files)>50: errors.append("More than 50 files")
    if total>2_000_000: errors.append("Total exceeds 2MB")
    skill=directory/"SKILL.md"
    if not skill.exists(): errors.append("Root SKILL.md is missing")
    else:
        text=skill.read_text(encoding="utf-8")
        if not text.startswith("---\n") or not re.search(r"^name:\s*\S+",text,re.M) or not re.search(r"^description:\s*\S+",text,re.M):
            errors.append("Invalid required frontmatter")
    output({"valid":not errors,"file_count":len(files),"total_bytes":total,"errors":errors,"files":files})
    return 0 if not errors else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    p=commands.add_parser("inspect"); p.add_argument("--case",required=True)
    p=commands.add_parser("search"); p.add_argument("directory"); p.add_argument("--query",required=True); p.add_argument("--limit",type=int,default=8); p.add_argument("--kind",choices=["block","line","table_cell","all"],default="block")
    p=commands.add_parser("check-result"); p.add_argument("--case",required=True); p.add_argument("--result",required=True)
    p=commands.add_parser("audit"); p.add_argument("directory")
    p=commands.add_parser("check-package"); p.add_argument("directory")
    args=parser.parse_args()
    try:
        if args.command=="inspect": return inspect_case(args.case)
        if args.command=="search": return search(args.directory,args.query,args.limit,args.kind)
        if args.command=="check-result": return check_result(args.case,args.result)
        if args.command=="audit": return audit(args.directory)
        if args.command=="check-package": return check_package(args.directory)
    except (OSError,ValueError,KeyError,TypeError) as error:
        output({"valid":False,"error":f"{type(error).__name__}: {str(error)[:500]}"})
        return 2


if __name__=="__main__":
    raise SystemExit(main())
