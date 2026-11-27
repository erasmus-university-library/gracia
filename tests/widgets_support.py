"""What the tests of the form widgets share: content types on small shapes of their own (a
`gadget` that has one property for each kind of widget, a `roster` that has references), and
a case that makes a repository of them and posts their forms."""

import re

from harness import FOAF_PERSON, shape_type
from test_web import HeaderPolicy, WebCase

from gracia.main import make_config

ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}
EX = "http://example.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

GADGET = shape_type(
    """
ex:GadgetShape a sh:NodeShape ; sh:targetClass ex:Gadget ;
    dash:propertyRole dash:IconRole ; sh:defaultValue "box" ;
    sh:property [ sh:path ex:name ; sh:name "name" ; sh:order 1 ;
        dash:propertyRole dash:LabelRole ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:alias ; sh:name "alias" ; sh:order 2 ;
        sh:or ( [ sh:datatype xsd:string ] [ sh:datatype rdf:langString ] ) ] ;
    sh:property [ sh:path ex:note ; sh:name "note" ; sh:order 3 ;
        dash:editor dash:TextAreaEditor ; sh:maxCount 1 ;
        sh:or ( [ sh:datatype xsd:string ] [ sh:datatype rdf:langString ] ) ] ;
    sh:property [ sh:path ex:started ; sh:name "start date" ; sh:order 4 ;
        sh:maxCount 1 ; sh:or ( [ sh:datatype xsd:date ] [ sh:datatype xsd:gYear ] ) ] ;
    sh:property [ sh:path ex:language ; sh:name "language" ; sh:order 5 ;
        dash:editor gracia:LanguageEditor ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:place ; sh:name "place" ; sh:order 6 ;
        sh:node ex:PlaceShape ] ;
    sh:property [ sh:path ex:mark ; sh:name "mark" ; sh:order 7 ;
        sh:node ex:MarkShape ] .

ex:PlaceShape a sh:NodeShape ; sh:class ex:Place ; sh:name "Place" ;
    dash:propertyRole dash:IconRole ; sh:defaultValue "geo-alt" ;
    gracia:headerTemplate ( "[{}, ][{} ][{}, ][{}]" ex:street ex:code ex:city ex:country ) ;
    sh:property [ sh:path ex:street ; sh:name "street" ; sh:order 1 ;
        sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:code ; sh:name "code" ; sh:order 2 ;
        sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:city ; sh:name "city" ; sh:order 3 ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:country ; sh:name "country" ; sh:order 4 ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] .

ex:MarkShape a sh:NodeShape ; sh:class ex:Mark ; sh:name "Mark" ;
    dash:propertyRole dash:IconRole ; sh:defaultValue "fingerprint" ;
    gracia:headerTemplate ( "{}" ex:value ) ;
    gracia:headerAddOnTemplate ( "{}" ex:kind ) ;
    sh:message "The value is not in the form of its kind: ORCID 0000-0002-1825-0097, ISNI 16 digits" ;
    sh:or (
        [ sh:property [ sh:path ex:kind ; sh:hasValue "ORCID" ] ;
          sh:property [ sh:path ex:value ; sh:pattern "^\\\\d{4}-\\\\d{4}-\\\\d{4}-\\\\d{3}[\\\\dX]$" ;
            sh:message "An ORCID iD is four groups of four digits, like 0000-0002-1825-0097." ] ;
          sh:property [ sh:path ex:url ; sh:pattern "^https://orcid\\\\.org/" ;
            sh:message "An ORCID URL starts with https://orcid.org/" ;
            gracia:propertyTemplate ( "https://orcid.org/{}" ex:value ) ] ]
        [ sh:property [ sh:path ex:kind ; sh:hasValue "ISNI" ] ;
          sh:property [ sh:path ex:value ; sh:pattern "^\\\\d{15}[\\\\dX]$" ;
            sh:message "An ISNI is 16 digits (the last may be X)." ] ;
          sh:property [ sh:path ex:url ; sh:pattern "^https://isni\\\\.org/" ;
            sh:message "An ISNI URL starts with https://isni.org/isni/" ;
            gracia:propertyTemplate ( "https://isni.org/isni/{}" ex:value ) ] ]
        [ sh:property [ sh:path ex:kind ; sh:in ( "Local" "External" ) ] ]
    ) ;
    sh:property [ sh:path ex:kind ; sh:name "kind" ; sh:order 1 ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ;
        sh:in ( "ORCID" "ISNI" "Local" "External" ) ] ;
    sh:property [ sh:path ex:value ; sh:name "value" ; sh:order 2 ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:url ; sh:name "URL" ; sh:order 3 ;
        dash:editor dash:URIEditor ; sh:maxCount 1 ; sh:nodeKind sh:IRI ] .
""",
    name="Gadget",
    slug="gadget",
    class_uri=EX + "Gadget",
)

ROSTER = shape_type(
    """
ex:RosterShape a sh:NodeShape ; sh:targetClass ex:Roster ;
    sh:property [ sh:path ex:name ; sh:name "name" ; sh:order 1 ;
        dash:propertyRole dash:LabelRole ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] ;
    sh:property [ sh:path ex:members ; sh:name "member" ; sh:order 2 ;
        sh:minCount 1 ; sh:maxCount 1 ;
        dash:editor gracia:URIListEditor ; sh:node ex:PersonUriListShape ;
        sh:not [ sh:hasValue rdf:nil ] ] ;
    sh:property [ sh:path ex:host ; sh:name "host" ; sh:order 3 ;
        dash:editor dash:URIEditor ; sh:node ex:OrgShape ] ;
    sh:property [ sh:path ex:parent ; sh:name "parent" ; sh:order 4 ;
        dash:editor dash:URIEditor ; sh:nodeKind sh:IRI ] .

ex:PersonUriListShape a sh:NodeShape ;
    sh:or (
        [ sh:nodeKind sh:IRI ; sh:hasValue rdf:nil ]
        [ sh:nodeKind sh:BlankNodeOrIRI ;
          sh:property [ sh:path rdf:first ; sh:minCount 1 ; sh:maxCount 1 ;
              sh:nodeKind sh:IRI ; sh:node gracia:PersonMinimalShape ] ;
          sh:property [ sh:path rdf:rest ; sh:minCount 1 ; sh:maxCount 1 ;
              sh:node ex:PersonUriListShape ] ]
    ) .
""",
    name="Roster",
    slug="roster",
    class_uri=EX + "Roster",
)

ORG = shape_type(
    """
ex:Org rdfs:label "Organization" .
ex:University rdfs:label "College or university" .
ex:OrgShape a sh:NodeShape ; sh:targetClass ex:Org , ex:University ;
    sh:name "Organization" ;
    dash:propertyRole dash:IconRole ; sh:defaultValue "building" ;
    sh:property [ sh:path ex:name ; sh:name "name" ; sh:order 1 ;
        dash:propertyRole dash:LabelRole ;
        sh:minCount 1 ; sh:maxCount 1 ; sh:datatype xsd:string ] .
""",
    name="Organization",
    slug="org",
    class_uri=EX + "Org",
)

PEOPLE = [f"ark:/99999/ir/person/{n}" for n in range(1, 5)]
FOAF_PERSON_CLASS = FOAF_PERSON.class_uri


class FormCase(WebCase):
    """A repository `test_repo` of the types in `types`, and a client that is the admin."""

    types = (GADGET,)  # noqa: RUF012

    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()  # the registry, for code that runs outside a request
        self.addCleanup(self.config.end)
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse battery"),
            content_types=list(self.types),
        )
        self.web = self.client(self.config)

    def page(self, path: str) -> str:
        return self.web.get(path, headers=ADMIN).text

    def posted(self, path: str, rows=None, mappings=None, **values):
        """What the form of the path posts: the fields as they are, but for `values` (a
        field by name; the name of a record is "A gadget" unless it is given), the
        sequences in `rows` (a sequence by name: its rows, each a text, or a list of
        (field, value) for a node) and the mappings in `mappings` (a mapping by name: its
        list of (field, value))."""
        rows = rows or {}
        mappings = mappings or {}
        form = self.web.get(path, headers=ADMIN).forms[0]
        out = []
        inside = 0  # how deep in what is replaced
        for name, value in form.submit_fields("save"):
            if inside:
                if name == "__start__":
                    inside += 1
                if name == "__end__":
                    inside -= 1
                if inside == 0:
                    out.append((name, value))
                continue
            if name == "__start__" and value.removesuffix(":sequence") in rows:
                out.append((name, value))
                for row in rows[value.removesuffix(":sequence")]:
                    if isinstance(row, str):
                        out.append(("item", row))
                    else:
                        out += [("__start__", "item:mapping"), *row]
                        out.append(("__end__", "item:mapping"))
                inside = 1
                continue
            if name == "__start__" and value.removesuffix(":mapping") in mappings:
                out += [(name, value), *mappings[value.removesuffix(":mapping")]]
                inside = 1
                continue
            if name in values:
                value = values[name]
            if name == "name" and not value:
                value = "A gadget"
            out.append((name, value))
        return out

    def post(self, path: str, rows=None, mappings=None, **values):
        return self.web.post(
            path,
            params=self.posted(path, rows, mappings, **values),
            headers=POSTING,
            expect_errors=True,
        )

    def saved(self, path: str, rows=None, mappings=None, **values):
        """Posts the form, and says that it was saved."""
        response = self.post(path, rows, mappings, **values)
        self.assertEqual(response.status_int, 302, response.text[:500])
        return response

    def record(self, slug: str = "gadget", number: int = 1):
        return self.application["test_repo"].types[slug].find_record(str(number))

    def values(self, predicate: str, slug: str = "gadget", number: int = 1) -> list:
        record = self.record(slug, number)
        return record.graph.objects(record.resource, EX + predicate)


def sequence_params(params, sequence: str, action: str, index=None):
    """What a button of a form asks of the server (htmx): the form as it stands, and what
    to do with its sequence."""
    out = [*params, ("sequence", f"resource/{sequence}"), ("action", action)]
    if index is not None:
        out.append(("index", str(index)))
    return out


def inputs(html: str, name: str, attribute: str = "value") -> list[str]:
    """The `attribute` of the inputs that have the name, in the order of the page."""
    found = re.findall(rf'<input[^>]*name="{name}"[^>]*{attribute}="([^"]*)"', html)
    return found
