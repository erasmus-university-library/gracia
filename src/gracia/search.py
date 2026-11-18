import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyoxigraph as ox
import tantivy

log = logging.getLogger("gracia.search")

FIELD_TYPES = ("integer", "text", "date", "boolean", "json")
HEAP = 50_000_000  # bytes a writer may use for a batch
SEARCHED = ("label", "slug", "text", "record", "resource_uri")  # what a query is looked for in, best first
BUILDING = ".building-"  # the end of the directory an index is made in, with the token of the job


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # one of FIELD_TYPES
    stored: bool = True
    indexed: bool = True
    fast: bool = False  # a column to sort and filter by
    tokenizer: str = "default"  # text: default, raw (the whole value), en_stem, whitespace

    def as_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type, "stored": self.stored,
            "indexed": self.indexed, "fast": self.fast, "tokenizer": self.tokenizer,
        }


@dataclass(frozen=True)
class IndexDefinition:
    name: str
    fields: tuple[Field, ...]

    def as_json(self) -> str:
        return json.dumps({"name": self.name, "fields": [f.as_dict() for f in self.fields]})

    def hash(self) -> str:
        """Of the definition: what an index is made for."""
        return hashlib.sha256(self.as_json().encode()).hexdigest()[:16]

    def schema(self) -> tantivy.Schema:
        builder = tantivy.SchemaBuilder()
        for f in self.fields:
            if f.type == "text":
                # a text field is always indexed (`indexed` is for the other types), and can be fast too
                builder.add_text_field(f.name, stored=f.stored, fast=f.fast, tokenizer_name=f.tokenizer)
            elif f.type == "integer":
                builder.add_integer_field(f.name, stored=f.stored, indexed=f.indexed, fast=f.fast)
            elif f.type == "boolean":
                builder.add_boolean_field(f.name, stored=f.stored, indexed=f.indexed, fast=f.fast)
            elif f.type == "date":
                builder.add_date_field(f.name, stored=f.stored, indexed=f.indexed, fast=f.fast)
            elif f.type == "json":
                builder.add_json_field(f.name, stored=f.stored, fast=f.fast)
            else:
                raise ValueError(f"{self.name}: field {f.name} is of an unknown type {f.type!r}")
        return builder.build()


# the index of every record: what a person looks for in any of them (the fields every doc has)
RECORDS = IndexDefinition(
    "records",
    (
        Field("record_id", "integer", fast=True),  # what a record is deleted by
        Field("record", "text", tokenizer="raw"),
        Field("type", "text", tokenizer="raw"),
        Field("slug", "text", tokenizer="raw"),
        Field("label", "text"),
        Field("text", "text", stored=False),
        Field("resource_uri", "text", tokenizer="raw"),
        Field("published", "boolean", fast=True),
        Field("modified", "date", fast=True),
    ),
)

# what the index and the search page cannot do without: a record is deleted by its id, and a hit is a record
# of a type that is or is not published
REQUIRED_FIELDS = ("record_id", "record", "type", "published")


def definitions_for(repository) -> list[IndexDefinition]:
    """The indexes of a repository (the node): what its Search extension says (see
    `gracia.extensions.search`), none when it is off. Without the extension in the registry (a bare
    one, in a test) it is the index of every record."""
    try:
        return repository.extension("search").definitions()
    except KeyError:
        return [RECORDS]


# ---- the schema the docs offer, against the definition ---------------------------------------


def compare(offered: list[dict], definition: IndexDefinition | None) -> list[dict]:
    """The fields the docs can have (`offered`: `DocStore.schema`) against the fields of a definition: a row
    for each, the offered ones first in their order, then what only the definition has. A row is the field
    (`name`, `type`, `stored`, `indexed`, `fast`, `tokenizer`: the definition's when it has the field, else the
    offered one), `types` (the types whose docs have it), `required`, and a `state`:

    * `indexed`    the definition has it as the docs offer it
    * `different`  the definition has it otherwise (`differences` names what), so it is as the definition says
    * `available`  only the docs have it: it is not in the index
    * `orphan`     only the definition has it: no doc stylesheet makes it
    """
    have = {f.name: f.as_dict() for f in definition.fields} if definition else {}
    rows, seen = [], set()
    for field in offered:
        name = field["name"]
        seen.add(name)
        mine = have.get(name)
        row = {**(mine or {k: v for k, v in field.items() if k not in ("types", "conflict")}), "types": field.get("types", [])}
        row["conflict"] = bool(field.get("conflict"))
        row["differences"] = [] if mine is None else [k for k in mine if mine[k] != field.get(k)]
        row["state"] = "available" if mine is None else ("different" if row["differences"] else "indexed")
        row["required"] = name in REQUIRED_FIELDS
        rows.append(row)
    for name, mine in have.items():
        if name not in seen:
            rows.append({**mine, "types": [], "conflict": False, "differences": [], "state": "orphan", "required": name in REQUIRED_FIELDS})
    return rows


# ---- docs ------------------------------------------------------------------------------------


def _datetime(value) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def check_document(definition: IndexDefinition, document: dict) -> list[str]:
    """What is wrong with a doc for this schema, one text each (none: it is right). A field the schema
    does not have is not wrong: it is left out (`_for_tantivy`)."""
    if "error" in document:
        return [f"the stylesheet could not make it: {document['error']}"]
    problems = []
    fields = {f.name: f for f in definition.fields}
    for name, value in document.items():
        field = fields.get(name)
        if field is None:
            continue
        for item in value if isinstance(value, list) and field.type != "json" else [value]:
            if item is None:
                problems.append(f"{name}: no value")
            elif field.type == "integer" and (isinstance(item, bool) or not isinstance(item, int)):
                problems.append(f"{name}: {item!r} is not an integer")
            elif field.type == "boolean" and not isinstance(item, bool):
                problems.append(f"{name}: {item!r} is not a boolean")
            elif field.type == "text" and not isinstance(item, str):
                problems.append(f"{name}: {item!r} is not text")
            elif field.type == "json" and not isinstance(item, dict | list):
                problems.append(f"{name}: {item!r} is not an object")
            elif field.type == "date":
                try:
                    _datetime(item)
                except (TypeError, ValueError):
                    problems.append(f"{name}: {item!r} is not a date")
    if not isinstance(document.get("record_id"), int):
        problems.append("record_id: every document needs the id of its record")
    return problems


def _for_tantivy(definition: IndexDefinition, document: dict) -> dict:
    """The doc as Tantivy takes it: only the fields of the schema, and dates are datetimes."""
    dates = {f.name for f in definition.fields if f.type == "date"}
    names = {f.name for f in definition.fields}
    return {
        name: ([_datetime(v) for v in value] if isinstance(value, list) else _datetime(value))
        if name in dates
        else value
        for name, value in document.items()
        if name in names
    }


# ---- the indexes -----------------------------------------------------------------------------


class Indexes:
    """The indexes in a directory, `<directory>/<repository>/<name>`."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, repo: str, definition: IndexDefinition) -> Path:
        return self.directory / repo / definition.name

    def _meta(self, repo: str, definition: IndexDefinition) -> Path:
        return self.directory / repo / f"{definition.name}.json"

    def status(self, repo: str, definition: IndexDefinition) -> str:
        """`missing` (not made yet), `ok`, or `stale` (made for another definition)."""
        if not self.path(repo, definition).exists():
            return "missing"
        try:
            stored = json.loads(self._meta(repo, definition).read_text())["hash"]
        except (OSError, ValueError, KeyError):
            return "stale"
        return "ok" if stored == definition.hash() else "stale"

    def open(self, repo: str, definition: IndexDefinition) -> tantivy.Index | None:
        """The index, made when it is not there; None when it is stale (see the module)."""
        state = self.status(repo, definition)
        if state == "stale":
            return None
        path = self.path(repo, definition)
        if state == "missing":
            path.mkdir(parents=True, exist_ok=True)
            self._meta(repo, definition).write_text(
                json.dumps({"hash": definition.hash(), "definition": json.loads(definition.as_json())})
            )
        return tantivy.Index(definition.schema(), path=str(path), reuse=True)

    def reset(self, repo: str, definition: IndexDefinition) -> None:
        """Take an index away (the next write makes it again, empty)."""
        shutil.rmtree(self.path(repo, definition), ignore_errors=True)
        self._meta(repo, definition).unlink(missing_ok=True)

    @staticmethod
    def _prepare(definition: IndexDefinition, docs) -> tuple[list[dict], dict[str, list[str]]]:
        """The docs as Tantivy takes them, and the problems by record: the docs that are not right are
        left out."""
        made, problems = [], {}
        for doc in docs:
            found = check_document(definition, doc)
            if found:
                problems[doc.get("record", "?")] = found
            else:
                made.append(_for_tantivy(definition, doc))
        return made, problems

    @staticmethod
    def _write(index: tantivy.Index, deleted, made: list[dict]) -> None:
        """Delete the records `deleted` (ids) and add the documents, with one commit. A record is
        deleted and then added again, so it is the same for one that changed and one that is new."""
        writer = index.writer(heap_size=HEAP, num_threads=1)
        for record_id in deleted:
            writer.delete_documents_by_term("record_id", record_id)
        for document in made:
            writer.add_document(tantivy.Document.from_dict(document))
        writer.commit()

    def update(self, repo: str, definition: IndexDefinition, changes, docs: dict[str, dict]) -> dict:
        """Put the changes of a batch in the index with one commit. `changes` are `gracia.actions.Change`s,
        `docs` the docs by record uri (a record of the changes that is not deleted and has no doc is left
        as it is). Returns what was done: `{"indexed": n, "deleted": n, "problems": {record: [text]}}`."""
        if not changes:
            return {"indexed": 0, "deleted": 0, "problems": {}, "state": "ok"}
        index = self.open(repo, definition)
        if index is None:
            log.warning("the index %s of %s is stale: it has to be made again", definition.name, repo)
            return {"indexed": 0, "deleted": 0, "problems": {}, "state": "stale"}
        alive = [docs[c.record] for c in changes if c.action != "delete" and c.record in docs]
        made, problems = self._prepare(definition, alive)
        self._write(index, [c.record_id for c in changes], made)
        for record, found in problems.items():
            log.warning("%s of %s: %s", record, repo, "; ".join(found))
        return {
            "indexed": len(made),
            "deleted": sum(c.action == "delete" for c in changes),
            "problems": problems,
            "state": "ok",
        }

    # ---- making an index again ---------------------------------------------------------------
    # An index that is stale, or missing records, is made again next to the one that answers: a batch at a
    # time into `<name>.building-<token>`, and when every batch is in, the new directory takes the place of
    # the old (`swap`, which the caller does while it holds the lock of the repository's queue, so that the
    # worker is not writing to the old one then). What was written while it was made is queued again for the
    # worker afterwards (`Store.requeue_changes_since`).

    def building(self, repo: str, definition: IndexDefinition, token: str) -> Path:
        return self.directory / repo / f"{definition.name}{BUILDING}{token}"

    def build_batch(self, repo: str, definition: IndexDefinition, token: str, docs) -> dict:
        """Add the docs of a batch to the index that is being made. One writer at a time on a directory:
        the caller sees to that."""
        path = self.building(repo, definition, token)
        path.mkdir(parents=True, exist_ok=True)
        index = tantivy.Index(definition.schema(), path=str(path), reuse=True)
        made, problems = self._prepare(definition, docs)
        self._write(index, [], made)
        for record, found in problems.items():
            log.warning("%s of %s: %s", record, repo, "; ".join(found))
        return {"indexed": len(made), "problems": problems}

    def swap(self, repo: str, definition: IndexDefinition, token: str) -> None:
        """The index that was made takes the place of the one that answers (and is the one it is made for:
        the hash is written). Other indexes that were being made of this one are taken away."""
        live, building = self.path(repo, definition), self.building(repo, definition, token)
        if not building.exists():  # nothing had a place in it: it is there, and empty
            building.mkdir(parents=True)
            tantivy.Index(definition.schema(), path=str(building), reuse=True)
        old = live.with_name(f"{live.name}.old-{token}")
        if live.exists():
            live.rename(old)
        building.rename(live)
        self._meta(repo, definition).write_text(
            json.dumps({"hash": definition.hash(), "definition": json.loads(definition.as_json())})
        )
        shutil.rmtree(old, ignore_errors=True)
        for other in live.parent.glob(f"{definition.name}{BUILDING}*"):
            shutil.rmtree(other, ignore_errors=True)

    def stored_definition(self, repo: str, definition: IndexDefinition) -> IndexDefinition | None:
        """The definition the index on disk was made for (its `<name>.json`), None when there is no such
        index or its file cannot be read."""
        try:
            fields = json.loads(self._meta(repo, definition).read_text())["definition"]["fields"]
            return IndexDefinition(definition.name, tuple(Field(**field) for field in fields))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def find(self, repo: str, definition: IndexDefinition, text: str, limit: int = 20) -> tuple[int, list[dict]]:
        """Search an index: `(how many records match, the best `limit` of them)`, each as the stored
        fields of its document and a `score`. A query that is not well formed is read as well as it can
        be (the rest is left out). An index that is stale (made for other fields than the definition has)
        keeps answering what it had, with the fields it was made for. `LookupError` when the index is not
        there, or is stale and does not say what it was made for."""
        state = self.status(repo, definition)
        if state == "missing":
            raise LookupError(f"the index {definition.name} is missing")
        if state == "stale":
            stored = self.stored_definition(repo, definition)
            if stored is None:
                raise LookupError(f"the index {definition.name} is stale")
            definition = stored
        index = tantivy.Index.open(str(self.path(repo, definition)))
        index.reload()
        searcher = index.searcher()
        names = {f.name for f in definition.fields if f.type == "text"}
        fields = [name for name in SEARCHED if name in names]
        query, _errors = index.parse_query_lenient(text, fields, field_boosts={"label": 3.0, "slug": 2.0})
        found = searcher.search(query, limit)
        hits = []
        for score, address in found.hits:
            stored = {name: values[0] for name, values in searcher.doc(address).to_dict().items() if values}
            hits.append({**stored, "score": score})
        return found.count, hits

    def field_stats(self, repo: str, definition: IndexDefinition) -> dict[str, int | None]:
        """How many documents of an index have a value, for each field of the definition. Tantivy can count
        only the fast fields (a query for the existence of a value needs the column), so a field that is not
        one is None; so is every field of an index that is not there or is not the one for this definition."""
        if self.status(repo, definition) != "ok":
            return {f.name: None for f in definition.fields}
        index = tantivy.Index.open(str(self.path(repo, definition)))
        index.reload()
        searcher = index.searcher()
        found = {}
        for field in definition.fields:
            try:
                found[field.name] = searcher.search(tantivy.Query.exists_query(field.name), 1).count if field.fast else None
            except ValueError:  # not a column after all (a text field that is not fast)
                found[field.name] = None
        return found

    def stats(self, repo: str, definitions=None) -> list[dict]:
        """One row for each of the `definitions` (the index of every record when there are none given):
        its name, the state, the documents it holds and the space it takes."""
        rows = []
        for definition in definitions if definitions is not None else [RECORDS]:
            state, path = self.status(repo, definition), self.path(repo, definition)
            documents_, size = 0, 0
            if state != "missing":
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                index = tantivy.Index.open(str(path))
                index.reload()
                documents_ = index.searcher().num_docs
            rows.append(
                {"name": definition.name, "state": state, "documents": documents_, "bytes": size,
                 "path": str(path)}
            )
        return rows


def on_records_changed(event) -> None:
    """The subscriber of `gracia.actions.RecordsChanged`: the batch goes into every index of the
    repository."""
    indexes = event.application.indexes
    if indexes is None:
        return
    from gracia.resources import Repository

    repository = Repository(event.repo, event.application)  # not `application[...]`: no listing of repositories
    docs = dict(event.docs)
    try:
        definitions = definitions_for(repository)
        missing = {c.record: c.record_id for c in event.changes if c.action != "delete" and c.record in event.graphs and c.record not in docs}
        if definitions and missing:  # there is no doc cache, or the docs of the worker could not be made: make them here
            merged = ox.Dataset(quad for record in missing for quad in event.graphs[record])
            docs.update(event.application.cache.docs.bulk_update(repository, missing, ox.serialize(merged, format=ox.RdfFormat.RDF_XML).decode()))
    finally:
        repository.close()
    if not definitions:
        return
    for definition in definitions:
        indexes.update(event.repo, definition, event.changes, docs)
