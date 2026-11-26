import re
import time
from datetime import UTC, datetime

import pyoxigraph as ox
from harness import FOAF_PERSON
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import uri
from gracia.main import make_config


def naive_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}
XSD = "http://www.w3.org/2001/XMLSchema#"


class TimesCase(WebCase):
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
            content_types=[FOAF_PERSON],
        )
        self.web = self.client(self.config)

    def add_person(self, name="Ada"):
        form = self.web.get("/test_repo/person/add", headers=ADMIN).forms[0]
        form["name"] = name
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def edit_person(self, number, **fields):
        form = self.web.get(f"/test_repo/person/{number}/edit", headers=ADMIN).forms[0]
        for name, value in fields.items():
            form[name] = value
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def triples(self, record, predicate):
        with self.application.new_store("test_repo") as store:
            data = store.get(record)
        return [
            (o.value, o.datatype.value)
            for o in data.objects(ox.NamedNode(record), ox.NamedNode(predicate))
        ]

    def row(self, record):
        with self.application.new_store("test_repo") as store:
            cursor = store.con.execute(
                "SELECT * FROM records WHERE record_id = record_id(?)", [record]
            )
            row = cursor.fetchone()
            return dict(zip([d[0] for d in cursor.description], row, strict=False))

    def set_timezone(self, name):
        form = self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).forms[
            "form-repository-settings"
        ]
        form["timezone"] = name
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)


class StampsTest(TimesCase):
    def test_a_new_record_has_both_times_in_utc_and_they_are_the_columns_of_records(
        self,
    ):
        self.add_person()
        for predicate, column in (
            (uri.CREATED_AT, "created_at"),
            (uri.MODIFIED_AT, "modified_at"),
        ):
            [(text, datatype)] = self.triples("rec:person:1", predicate)
            self.assertEqual(datatype, XSD + "dateTime")
            self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
            self.assertEqual(naive_utc(text), self.row("rec:person:1")[column])
        self.assertEqual(
            self.triples("rec:person:1", uri.CREATED_AT),
            self.triples("rec:person:1", uri.MODIFIED_AT),
        )

    def test_a_change_moves_modified_at_and_keeps_created_at(self):
        self.add_person()
        before = self.row("rec:person:1")
        time.sleep(0.01)
        self.edit_person(1, name="Ada Lovelace")
        after = self.row("rec:person:1")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertGreater(after["modified_at"], before["modified_at"])
        [(text, _)] = self.triples("rec:person:1", uri.MODIFIED_AT)
        self.assertEqual(naive_utc(text), after["modified_at"])

    def test_a_save_that_changes_nothing_is_no_modification(self):
        self.add_person()
        before = self.row("rec:person:1")
        time.sleep(0.01)
        self.edit_person(1)
        self.assertEqual(self.row("rec:person:1")["modified_at"], before["modified_at"])


class ModifiedAtIsNeverGivenTest(TimesCase):
    def import_person(self, name: str, modified: str, number: int = 7) -> None:
        text = (
            f"@prefix gracia: <https://gracia.pub/ns/> . @prefix xsd: <{XSD}> . "
            f"@prefix foaf: <http://xmlns.com/foaf/0.1/> .\n"
            f"<rec:person:{number}> a gracia:Record ; gracia:modified_at "
            f'"{modified}"^^xsd:dateTime ; '
            f'gracia:resource [ a foaf:Person ; foaf:name "{name}" ] .'
        )
        node = self.application[
            "test_repo"
        ]  # its store is the configured one: the hooks of the predicates run
        self.addCleanup(node.close)
        node.store.put_multi(
            ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))
        )

    def modified(self, number: int = 7):
        return self.row(f"rec:person:{number}")["modified_at"]

    def test_a_new_record_gets_the_time_of_the_write_whatever_its_text_says(self):
        for number, given in (
            (7, "2001-01-01T00:00:00.000000Z"),
            (8, "2999-01-01T00:00:00.000000Z"),
        ):
            self.import_person("Ada", given, number)
            self.assertLess(
                abs((self.modified(number) - datetime.utcnow()).total_seconds()),  # ty: ignore[deprecated]
                60,
            )
            [(text, _)] = self.triples(f"rec:person:{number}", uri.MODIFIED_AT)
            self.assertEqual(naive_utc(text), self.modified(number))

    def test_a_change_moves_it_up_whatever_its_text_says(self):
        self.import_person("Ada", "2001-01-01T00:00:00.000000Z")
        first = self.modified()
        time.sleep(0.01)
        self.import_person(
            "Ada Lovelace", "2000-01-01T00:00:00.000000Z"
        )  # an earlier time
        second = self.modified()
        self.assertGreater(second, first)
        time.sleep(0.01)
        self.import_person("Ada King", "2999-01-01T00:00:00.000000Z")  # a later one
        third = self.modified()
        self.assertGreater(third, second)
        self.assertLess(abs((third - datetime.utcnow()).total_seconds()), 60)  # ty: ignore[deprecated]

    def test_a_text_that_changes_nothing_keeps_the_time_whatever_it_says(self):
        self.import_person("Ada", "2001-01-01T00:00:00.000000Z")
        first = self.modified()
        time.sleep(0.01)
        self.import_person("Ada", "2999-01-01T00:00:00.000000Z")
        self.assertEqual(self.modified(), first)

    def test_a_record_deleted_and_made_again_is_not_modified_earlier_than_it_was(self):
        self.import_person("Ada", "2001-01-01T00:00:00.000000Z")
        first = self.modified()
        self.application["test_repo"].store.delete(["rec:person:7"])
        time.sleep(0.01)
        self.import_person("Ada", "2001-01-01T00:00:00.000000Z")
        self.assertGreater(self.modified(), first)


class IsPublishedTest(TimesCase):
    def test_it_is_true_until_the_checkbox_is_unchecked(self):
        self.add_person()
        self.assertEqual(
            self.triples("rec:person:1", uri.IS_PUBLISHED), [("true", XSD + "boolean")]
        )
        self.assertIs(self.row("rec:person:1")["is_published"], True)
        self.edit_person(1, is_published=False)
        self.assertEqual(
            self.triples("rec:person:1", uri.IS_PUBLISHED), [("false", XSD + "boolean")]
        )
        self.assertIs(self.row("rec:person:1")["is_published"], False)
        self.edit_person(1, is_published=True)
        self.assertIs(self.row("rec:person:1")["is_published"], True)

    def test_a_record_that_says_nothing_of_it_is_published(self):
        self.add_person()
        with self.application.new_store("test_repo") as store:
            store.con.execute(
                "DELETE FROM triples WHERE s = 'rec:person:1' AND p = ?",
                [uri.IS_PUBLISHED],
            )
        self.assertIs(
            self.row("rec:person:1")["is_published"], True
        )  # the column's default too

    def test_the_form_has_a_checkbox(self):
        page = self.web.get("/test_repo/person/add", headers=ADMIN).text
        self.assertRegex(
            page, r'type="checkbox"\s+name="is_published"[^>]*checked'
        )  # new: checked


class TimezoneTest(TimesCase):
    def test_the_settings_tab_has_a_drop_down_of_zones_that_can_be_empty(self):
        page = self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).text
        select = must_search(r'<select name="timezone".*?</select>', page, re.S)[0]
        self.assertIn(
            '<option selected="selected" value="">(none: UTC)</option>', select
        )
        self.assertIn('<optgroup label="Europe">', select)
        self.assertIn('<option value="Europe/Amsterdam">Amsterdam</option>', select)
        self.assertNotIn('value="US/Eastern"', select)

    def test_choosing_a_zone_stores_its_name_and_empty_removes_it(self):
        self.set_timezone("Europe/Amsterdam")
        self.assertEqual(self.triples_of_repository(), ["Europe/Amsterdam"])
        self.set_timezone("")
        self.assertEqual(self.triples_of_repository(), [])

    def triples_of_repository(self):
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:repository:1")
        content = data.value(
            ox.NamedNode("rec:repository:1"), ox.NamedNode(uri.RESOURCE)
        )
        return [o.value for o in data.objects(content, ox.NamedNode(uri.TIMEZONE))]

    def test_a_name_that_is_not_a_zone_is_refused(self):
        form = self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).forms[
            "form-repository-settings"
        ]
        form["timezone"].force_value("Mars/Olympus")
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        self.assertEqual(self.triples_of_repository(), [])


class RepositoryZoneTest(TimesCase):
    def zone(self):
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        return node.timezone()

    def test_the_zone_of_the_repository_is_utc_for_none_and_for_a_name_the_system_does_not_know(  # noqa: E501
        self,
    ):
        self.assertIs(self.zone(), UTC)
        self.set_timezone("Europe/Amsterdam")
        self.assertEqual(str(self.zone()), "Europe/Amsterdam")
        self.set_timezone("")
        self.assertIs(self.zone(), UTC)
        # a name that was stored, and is not a zone here
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        record = node.record()
        subject, resource = record.resource, record.graph
        resource.add(
            ox.Quad(
                subject,
                ox.NamedNode(uri.TIMEZONE),
                ox.Literal("Nowhere/Land"),
                ox.DefaultGraph(),
            )
        )
        record.save()
        fresh = self.application["test_repo"]
        self.addCleanup(fresh.close)
        fresh_record = fresh.record()
        subject, resource = fresh_record.resource, fresh_record.graph
        self.assertEqual(
            [o.value for o in resource.objects(subject, ox.NamedNode(uri.TIMEZONE))],
            ["Nowhere/Land"],
        )  # it is stored
        self.assertIs(self.zone(), UTC)
