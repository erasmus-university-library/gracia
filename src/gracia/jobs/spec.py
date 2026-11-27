import re

import colander
import luigi

from gracia.jobs.stage import (JobTask, Stage)

MAX_STAGES = 20
REPO_ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class StageNode(colander.MappingSchema):
    stage = colander.SchemaNode(colander.String(), validator=colander.Length(min=1))
    params = colander.SchemaNode(colander.Mapping(unknown="preserve"), missing=colander.drop)


class Stages(colander.SequenceSchema):
    stage = StageNode()


class JobNode(colander.MappingSchema):
    repo = colander.SchemaNode(
        colander.String(), validator=colander.Regex(REPO_ID, msg="Not a repository id")
    )
    job = colander.SchemaNode(colander.String(), missing=None)
    subject = colander.SchemaNode(colander.String(), missing=None)
    stages = Stages(validator=colander.Length(min=1, max=MAX_STAGES))


class JobSpecError(ValueError):

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{where}: {why}" for where, why in errors.items()))


RESERVED = frozenset({"job_dir", "repo", "n", "chain"})  # Luigi parameters the job sets itself


def _check_stage(repo: str, index: int, entry: dict, before: list, errors: dict) -> None:
    name = entry["stage"]
    try:
        cls = Stage.named(name)
    except KeyError:
        known = ", ".join(Stage.known()) or "none"
        errors[f"stages.{index}.stage"] = f"No such stage: {name!r} (known: {known})"
        return
    if (index == 0) != bool(cls.first):
        errors[f"stages.{index}.stage"] = (
            f"{name} must come first"
            if cls.first
            else f"{name} cannot be the first stage: something has to make the items"
        )
        return
    declared = {k: v for k, v in cls.get_params() if k not in RESERVED}
    typed: dict = {}
    for key, value in entry.get("params", {}).items():
        param = declared.get(key)
        if param is None:
            errors[f"stages.{index}.params"] = f"unknown parameter {key}"
            return
        try:  # Luigi parses text; a value from JSON is taken through the same door
            typed[key] = param.parse(param.serialize(value))
        except (ValueError, TypeError):
            errors[f"stages.{index}.params.{key}"] = f"not a valid value: {value!r}"
            return
    try:  # what Luigi itself finds wrong: a parameter without a value
        cls(
            job_dir="",
            repo=repo,
            n=0,
            chain=JobTask.dumps([*before, {"stage": name, "params": typed}]),
            **typed,
        )
    except luigi.parameter.MissingParameterException as err:
        errors[f"stages.{index}.params"] = str(err).split(": ", 1)[-1]
        return
    entry["params"] = typed


def validate_spec(data) -> dict:
    try:
        spec = JobNode().deserialize(data)
    except colander.Invalid as invalid:
        raise JobSpecError(invalid.asdict()) from None
    errors: dict[str, str] = {}
    before: list = []
    for index, entry in enumerate(spec["stages"]):
        entry["params"] = dict(entry.get("params", {}))
        _check_stage(spec["repo"], index, entry, before, errors)
        before.append(entry)
    if errors:
        raise JobSpecError(errors)
    return spec
