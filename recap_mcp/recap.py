"""Pure logic: parse a recap definition and decide which fields are visible."""
from dataclasses import dataclass


@dataclass
class Field:
    instance_id: str
    field_id: str
    form_id: str
    block_id: str | None
    label: str
    type: str
    required: bool
    options: dict[str, str]  # active option name -> option id
    logic: list[list[tuple[str, str, str]]]  # groups of (ref_instance_id, logic, value)
    validations: list[tuple[str, str | None, str | None]]  # (type, ref_instance_id, value.strip())


def _live(x) -> bool:
    return x.get("is_archived") != "t"


def parse_definition(defn: dict) -> list[Field]:
    out: list[Field] = []

    def walk(items, form_id):
        for it in items:
            if not _live(it):
                continue
            if "field" not in it:  # section/form
                walk(it.get("items", []), it["id"])
                continue
            fd = it["field"]
            if not _live(fd):
                continue
            opts = fd.get("options") or []
            if isinstance(opts, dict):
                opts = list(opts.values())
            vals = [
                (v["validation_type"], v.get("references_events_custom_instances_id"),
                 v["value"].strip() if v.get("value") is not None else None)
                for v in it.get("validations", []) if _live(v)
            ]
            groups = [
                [(c["references_events_custom_instances_id"], c["logic"], c["value"])
                 for c in g.get("conditions", []) if _live(c)]
                for g in it.get("logic_groups", []) if _live(g)
            ]
            out.append(Field(
                instance_id=it["id"], field_id=fd["id"], form_id=form_id,
                block_id=it.get("events_custom_field_blocks_id"),
                label=fd["name"], type=fd["defined_as"],
                required=any(v[0] == "responseRequired" for v in vals),
                options={o["name"]: o["id"] for o in opts if o.get("active") == "t"},
                logic=[g for g in groups if g], validations=vals,
            ))

    for page in defn["pages"]:
        if _live(page):
            walk(page.get("items", []), "")
    return out


def _norm(v: str) -> str:
    return {"t": "1", "f": "0"}.get(v, v)


def _holds(c, answers: dict[str, str]) -> bool:
    ref, _, want = c
    got = answers.get(ref)
    if got is None:
        return False
    return _norm(want) in {_norm(p) for p in got.split("|")}  # multiselect: value in selected ids


def visible_ids(fields: list[Field], answers: dict[str, str]) -> set[str]:
    by_id = {f.instance_id: f for f in fields}
    memo: dict[str, bool] = {}

    def vis(iid: str) -> bool:
        if iid not in by_id:
            return False
        if iid in memo:
            return memo[iid]
        memo[iid] = False  # cycle guard
        f = by_id[iid]
        if not f.logic or any(c[1] != "EQUALS" for g in f.logic for c in g):
            r = True  # unknown operator: never hide a possibly-required field
        else:
            r = any(
                all(_holds(c, answers) and vis(c[0]) for c in g) for g in f.logic
            )
        memo[iid] = r
        return r

    return {f.instance_id for f in fields if vis(f.instance_id)}


def unknown_logic(fields: list[Field]) -> list[str]:
    return [f"{f.label}: {c[1]}" for f in fields for g in f.logic for c in g if c[1] != "EQUALS"]


def find(fields: list[Field], label_part: str) -> Field:
    hits = [f for f in fields if label_part.lower() in f.label.lower()]
    if len(hits) != 1:
        raise KeyError(f"{len(hits)} fields match {label_part!r}")
    return hits[0]
