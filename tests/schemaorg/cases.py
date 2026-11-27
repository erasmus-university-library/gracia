"""The base cases of the schema.org tests (copies of what the widget tests had: a repository
with the schema.org types, the forms of their fields)."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pyoxigraph as ox
from harness import BUNDLED, ConfiguredCase
from pyramid.registry import Registry
from test_manage import ADMIN as MANAGE_ADMIN
from test_manage import ManageCase
from test_web import HeaderPolicy, WebCase

from gracia.change_events import RecordsChanged, process_pending
from gracia.main import make_config
from gracia.search import Indexes, on_records_changed

AUTHOR = "https://schema.org/author"
UNIVERSITY = "https://schema.org/CollegeOrUniversity"
ADA, DELFT = "ark:/99999/ir/person/1", "ark:/99999/ir/organization/1"
ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}
PEOPLE = [f"ark:/99999/ir/person/{n}" for n in range(1, 5)]
ADD = "/work_repo/work/add"
EDIT = "/work_repo/work/{}/edit"
WORK = "/refs/work/add"
PREFIXES = """
@prefix g: <https://gracia.pub/ns/> . @prefix schema: <https://schema.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""
ROLE = "<http://example.org/vocab/role/%s>"
NUMBERS = (1, 2, 3, 4, 5)
HTML = {"Accept": "text/html"}


def ark(kind: str, number: int) -> str:
    return f"<ark:/99999/ir/{kind}/{number}>"


def work(number: int, kind: str, title: str, more: str = "") -> str:
    tail = f" ; {more}" if more else ""
    return record("work", number, f'a schema:{kind} ; schema:name "{title}"{tail}')


def record(kind: str, number: int, resource: str) -> str:
    """A record, as populate_schemaorg.sh makes them: resource uri and resource."""
    return (
        f"<rec:{kind}:{number}> a g:Record ; "
        f"g:resource_uri <ark:/99999/ir/{kind}/{number}> ; "
        f"g:resource [ {resource} ] .\n"
    )


def identifier(kind: str, value: str, url: str = "") -> str:
    more = f" ; schema:url <{url}>" if url else ""
    return (
        "schema:identifier [ a schema:PropertyValue ; "
        f'schema:propertyID "{kind}" ; schema:value "{value}"{more} ]'
    )


def role(name: str, kind: str, number: int) -> str:
    return (
        f"schema:contributor [ a schema:Role ; schema:roleName {ROLE % name} ; "
        f"schema:contributor {ark(kind, number)} ]"
    )


def person(number: int, name: str, given: str = "", family: str = "") -> str:
    parts = f' ; schema:givenName "{given}" ; schema:familyName "{family}"'
    return record(
        "person",
        number,
        f'a schema:Person ; schema:name "{name}"{parts if family else ""}',
    )


def series(number: int, kind: str, name: str, issn: str) -> str:
    return record(
        "series",
        number,
        f'a schema:{kind} ; schema:name "{name}" ; {identifier("ISSN", issn)}',
    )


BASE = "".join(
    (
        person(1, "Ada Lovelace", "Ada", "Lovelace"),
        person(2, "Alan Turing", "Alan", "Turing"),
        person(3, "Plato"),  # a name and no parts of it
        record(
            "organization",
            1,
            'a schema:CollegeOrUniversity ; schema:name "Delft University" ; '
            "schema:address [ a schema:PostalAddress ; "
            'schema:addressLocality "Delft" ]',
        ),
        series(1, "Periodical", "Journal of Engines", "0302-9743"),
        series(2, "BookSeries", "Lecture Notes in Computing", "1611-3349"),
        series(3, "Newspaper", "The Daily Gear", "1234-5679"),
        series(4, "CreativeWorkSeries", "Report Series", "2345-678X"),
    )
)
ARTICLE = work(
    1,
    "ScholarlyArticle",
    "On the Analytical Engine",
    f"schema:author ( {ark('person', 1)} {ark('person', 2)} ) ; "
    f"schema:publisher {ark('organization', 1)} ; "
    f"schema:isPartOf {ark('series', 1)} ; "
    'schema:datePublished "1843-07-01"^^xsd:date ; schema:volumeNumber "3" ; '
    'schema:issueNumber "12" ; schema:pageStart "666" ; schema:pageEnd "731" ; '
    'schema:inLanguage "en" ; schema:keywords "engines, computation" ; '
    'schema:abstract "Notes on a machine." ; '
    + identifier("DOI", "10.1000/xyz123", "https://doi.org/10.1000/xyz123"),
)
BARE = work(5, "Dataset", "Measurements")
THESIS = work(
    2,
    "Thesis",
    "Computable Numbers",
    f"schema:author ( {ark('person', 2)} ) ; "
    'schema:datePublished "2019"^^xsd:gYear ; '
    'schema:inSupportOf "Master of Science" ; schema:inLanguage "nl" ; '
    + role("ThesisSupervisor", "person", 1)
    + " ; "
    + role("DegreeGrantor", "organization", 1),
)
BOOK = work(
    3,
    "Book",
    "Notes on Computing",
    f"schema:author ( {ark('person', 2)} ) ; "
    f"schema:publisher {ark('organization', 1)} ; "
    f"schema:isPartOf {ark('series', 2)} ; "
    'schema:datePublished "2020-05"^^xsd:gYearMonth ; '
    + identifier("ISBN", "978-3-16-148410-0", "urn:isbn:978-3-16-148410-0"),
)
NEWS = work(
    4,
    "Article",
    "Engines Run Late",
    f'schema:isPartOf {ark("series", 3)} ; schema:datePublished "2021-01-02"^^xsd:date',
)


class DatesCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED["series"]],
        )
        self.web = self.client(self.config)

    def settings_form(self):
        return self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).forms[
            "form-repository-settings"
        ]

    def set_locale(self, code):
        form = self.settings_form()
        form["locale"] = code
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def series_form(self, path="/test_repo/series/add"):
        return self.web.get(path, headers=ADMIN).forms[0]

    def save(self, form):
        return form.submit("save", headers=POSTING, expect_errors=True)


class ListCase(DatesCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "work_repo",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED["work"]],
        )

    def fields(self, path=ADD, authors=(), name="A work"):
        """What the form of the path posts, with the authors as the rows of the list."""
        form = self.web.get(path, headers=ADMIN).forms[0]
        out, inside = [], False
        for field, value in form.submit_fields("save"):
            if (field, value) == ("__start__", "author:sequence"):
                inside = True
                out.append((field, value))
                out += [("item", author) for author in authors]
            elif (field, value) == ("__end__", "author:sequence"):
                inside = False
                out.append((field, value))
            elif not inside:
                if field == "type" and not value:
                    value = "https://schema.org/Thesis"
                out.append((field, name if field == "name" and not value else value))
        return out

    def post(self, path=ADD, authors=(), **kw):
        return self.web.post(
            path,
            params=self.fields(path, authors, **kw),
            headers=POSTING,
            expect_errors=True,
        )

    def work(self, number=1):
        return self.application["work_repo"].types["work"].find_record(str(number))

    def authors(self, number=1) -> list[str]:
        work = self.work(number)
        head = work.graph.objects(work.resource, AUTHOR)
        return [o.value for o in work.graph.walk_list(head[0])] if head else []


class ReferenceCase(ListCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "refs",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED[k] for k in ("work", "person", "organization")],
        )
        self._index = tempfile.TemporaryDirectory()
        self.addCleanup(self._index.cleanup)
        self.application.indexes = Indexes(Path(self._index.name))
        self.person("Ada Lovelace")
        self.person("Ada Byron")
        self.organization("Delft University")
        self.reindex()

    def create(self, kind: str, **fields) -> None:
        form = self.web.get(f"/refs/{kind}/add", headers=ADMIN).forms[0]
        for name, value in fields.items():
            form[name] = value
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def person(self, name: str) -> None:
        self.create("person", name=name)

    def organization(self, name: str) -> None:
        self.create("organization", name=name, type=UNIVERSITY)

    def reindex(self) -> None:
        registry = Registry()
        registry.registerHandler(on_records_changed, (RecordsChanged,))
        process_pending(self.application, registry)

    def lookup(self, text: str, *classes: str, who=ADMIN):
        params = [("q", text), *(("type", c) for c in classes)]
        return self.web.get(
            "/refs/lookup", params=params, headers=who, expect_errors=True
        )

    def found(self, text: str, *classes: str) -> list[str]:
        response = self.lookup(text, *classes)
        self.assertEqual(response.status_int, 200, response.text)
        return [i["label"] for i in response.json["items"]]

    def save_work(self, authors, publisher="", name="A work") -> None:
        params = self.fields(WORK, authors, name)
        if publisher:  # the first row of the bag of the publisher
            params = self.with_publisher(params, publisher)
        response = self.web.post(
            WORK, params=params, headers=POSTING, expect_errors=True
        )
        self.assertEqual(response.status_int, 302, response.text[:500])

    def with_publisher(self, params, publisher):
        seen, out = False, []
        for field, value in params:
            if (field, value) == ("__start__", "publisher:sequence"):
                seen = True
                out += [(field, value), ("item", publisher)]
            elif seen and (field, value) == ("item", ""):
                continue
            else:
                out.append((field, value))
        return out

    def work_page(self, number=1) -> str:
        return self.web.get(f"/refs/work/{number}/edit", headers=ADMIN).text


class WorkDocCase(ConfiguredCase):
    def setUp(self):
        super().setUp()
        kinds = ("work", "person", "organization", "series")
        self.application.create_repository(
            "lib",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED[k] for k in kinds],
        )
        self.put(BASE, ARTICLE, THESIS, BOOK, NEWS, BARE)

    def put(self, *turtle: str) -> None:
        text = (PREFIXES + "".join(turtle)).encode()
        data = ox.Dataset(ox.parse(text, format=ox.RdfFormat.TURTLE))
        with self.application.new_store("lib") as store:
            store.put_multi(data)

    def doc(self, number: int) -> dict:
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        found = repository.store.get_doc(f"rec:work:{number}")
        assert found is not None
        return found

    def csl(self, number: int) -> dict:
        blob = self.doc(number)["csl"]
        assert isinstance(blob, str)  # a string: the search index gets one term
        return json.loads(blob)


FILE = (
    f"schema:author ( {ark('person', 2)} ) ; "
    'schema:encoding [ a schema:MediaObject ; schema:name "paper.pdf" ; '
    "schema:contentUrl <https://example.org/paper.pdf> ; "
    'schema:encodingFormat "application/pdf" ; schema:contentSize "575kb" ]'
)


def labelled(kind: str, number: int, name: str, schema_class: str = "Person") -> str:
    """A record as a repository has it: with the label that its hooks make."""
    return (
        f'<rec:{kind}:{number}> a g:Record ; g:label "{name}" ; '
        f"g:resource_uri {ark(kind, number)} ; "
        f'g:resource [ a schema:{schema_class} ; schema:name "{name}" ] .\n'
    )


class WorkViewCase(ManageCase):
    def setUp(self):
        super().setUp()
        found = BUNDLED
        self.application.create_repository(
            "lib",
            admin=("admin", "correct horse battery"),
            content_types=[
                replace(found["work"], view="work-schemaorg-01.xslt"),
                found["person"],
                found["organization"],
                found["series"],
            ],
        )
        self.put(
            BASE,
            labelled("person", 1, "Ada Lovelace"),
            labelled("person", 2, "Alan Turing"),
            labelled("series", 1, "Journal of Engines", "Periodical"),
            labelled("organization", 1, "Delft University", "Organization"),
            ARTICLE,
            BARE,
            work(6, "Report", "A Report", FILE),
        )

    def put(self, *turtle: str) -> None:
        text = (PREFIXES + "".join(turtle)).encode()
        data = ox.Dataset(ox.parse(text, format=ox.RdfFormat.TURTLE))
        with self.application.new_store("lib") as store:
            store.put_multi(data)

    def get(self, path: str) -> str:
        return self.client().get(path, headers={**HTML, **MANAGE_ADMIN}).text
