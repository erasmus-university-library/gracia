"""Dates in the forms, as the locale of the repository (`gracia:locale`) writes them."""

import re
import unittest

import colander
import deform
import pyoxigraph as ox
from test_web import HeaderPolicy, WebCase

from gracia import dates, forms, types
from gracia.main import make_config

ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}
START = "https://schema.org/startDate"
XSD = "http://www.w3.org/2001/XMLSchema#"
SCHEMA = "https://schema.org/"


class DatesTest(unittest.TestCase):
    def test_a_date_is_shown_in_the_order_and_with_the_separator_of_the_locale(self):
        for locale, shown in (
            ("", "2024-03-15"),
            ("nl-NL", "15-03-2024"),
            ("en-US", "03/15/2024"),
            ("en-GB", "15/03/2024"),
            ("de-DE", "15.03.2024"),
            ("ja-JP", "2024/03/15"),
        ):
            with self.subTest(locale=locale):
                self.assertEqual(dates.display("2024-03-15", locale), shown)
                self.assertEqual(dates.parse(shown, locale), "2024-03-15")

    def test_a_month_and_a_year_alone_are_dates_too(self):
        self.assertEqual(dates.display("2024-03", "nl-NL"), "03-2024")
        self.assertEqual(dates.display("2024-03", "en-US"), "03/2024")
        self.assertEqual(dates.display("2024-03", "ja-JP"), "2024/03")
        self.assertEqual(dates.display("2024", "nl-NL"), "2024")
        self.assertEqual(dates.parse("3-2024", "nl-NL"), "2024-03")
        self.assertEqual(dates.parse("2024", "en-US"), "2024")

    def test_iso_is_understood_whatever_the_locale(self):
        self.assertEqual(dates.parse("2024-03-15", "en-US"), "2024-03-15")
        self.assertEqual(dates.parse("2024-03", "nl-NL"), "2024-03")

    def test_single_digits_are_filled_in(self):
        self.assertEqual(dates.parse("5-3-2024", "nl-NL"), "2024-03-05")
        self.assertEqual(dates.parse(" 3/5/2024 ", "en-US"), "2024-03-05")

    def test_the_datatype_is_what_the_text_is(self):
        self.assertEqual(dates.datatype_of("2024-03-15"), XSD + "date")
        self.assertEqual(dates.datatype_of("2024-03"), XSD + "gYearMonth")
        self.assertEqual(dates.datatype_of("2024"), XSD + "gYear")
        self.assertIsNone(dates.datatype_of("15-03-2024"))
        self.assertIsNone(dates.datatype_of(""))

    def test_what_is_not_a_date_is_refused_and_not_shown_changed(self):
        for text in (
            "",
            "soon",
            "15-03",
            "32-01-2024",
            "29-02-2023",
            "1-1-24",
            "0-0-2024",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                dates.parse(text, "nl-NL")
        self.assertEqual(dates.display("2023-02-29", "nl-NL"), "2023-02-29")
        self.assertEqual(dates.display("next week", "nl-NL"), "next week")

    def test_a_shape_that_wants_a_full_date_refuses_a_year(self):
        with self.assertRaises(ValueError):
            dates.parse("2024", "nl-NL", (dates.XSD_DATE,))
        self.assertEqual(
            dates.parse("15-03-2024", "nl-NL", (dates.XSD_DATE,)), "2024-03-15"
        )

    def test_the_placeholder_says_what_to_type(self):
        self.assertEqual(dates.placeholder("nl-NL"), "dd-mm-yyyy")
        self.assertEqual(dates.placeholder("en-US"), "mm/dd/yyyy")
        self.assertEqual(dates.placeholder("", (dates.XSD_YEAR,)), "yyyy")


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
            content_types=[types.available()["series"]],
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


class LocaleSettingTest(DatesCase):
    def test_the_settings_offer_the_formats_with_the_iso_one_chosen(self):
        page = self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).text
        select = re.search(r'(?s)<select[^>]*name="locale".*?</select>', page)
        assert select is not None
        self.assertIn('selected="selected" value="">(none: ISO 8601)', select[0])
        self.assertIn("Dutch (15-03-2024)", select[0])

    def test_the_locale_is_kept_and_a_name_that_is_none_of_them_is_refused(self):
        self.assertEqual(self.application["test_repo"].locale(), "")
        self.set_locale("nl-NL")
        self.assertEqual(self.application["test_repo"].locale(), "nl-NL")
        form = self.settings_form()
        form["locale"].force_value("xx-XX")
        self.assertEqual(self.save(form).status_int, 200)
        self.assertEqual(self.application["test_repo"].locale(), "nl-NL")


class DateFormTest(DatesCase):
    def test_a_date_is_typed_as_the_locale_writes_it_and_stored_as_a_typed_iso_date(
        self,
    ):
        self.set_locale("nl-NL")
        form = self.series_form()
        self.assertEqual(form["startDate"].attrs.get("placeholder"), "dd-mm-yyyy")
        form["name"] = "A series"
        form["startDate"] = "15-03-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 302, response.text[:600])
        series = self.application["test_repo"].types["series"]["1"]
        found = series.graph.objects(series.resource, START)
        self.assertEqual(
            [(o.value, o.datatype.value) for o in found],
            [("2024-03-15", XSD + "date")],
        )
        page = self.web.get("/test_repo/series/1/edit", headers=ADMIN).text
        self.assertIn('value="15-03-2024"', page)

    def test_a_year_is_stored_as_a_year(self):
        self.set_locale("nl-NL")
        form = self.series_form()
        form["name"] = "Series 1"
        form["startDate"] = "2024"
        self.assertEqual(self.save(form).status_int, 302)
        series = self.application["test_repo"].types["series"]["1"]
        found = series.graph.objects(series.resource, START)
        self.assertEqual(
            [(o.value, o.datatype.value) for o in found], [("2024", XSD + "gYear")]
        )

    def test_a_kind_the_shape_does_not_allow_is_refused(self):
        self.set_locale("nl-NL")  # a series starts on a date or in a year, not a month
        form = self.series_form()
        form["name"] = "A series"
        form["startDate"] = "03-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Give it as", response.text)

    def test_without_a_locale_dates_are_iso(self):
        form = self.series_form()
        self.assertEqual(form["startDate"].attrs.get("placeholder"), "yyyy-mm-dd")
        form["name"] = "A series"
        form["startDate"] = "2024-03-15"
        self.assertEqual(self.save(form).status_int, 302)

    def test_what_is_not_a_date_is_refused_and_stays_in_the_input(self):
        self.set_locale("nl-NL")
        form = self.series_form()
        form["name"] = "A series"
        form["startDate"] = "31-02-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a date that exists", response.text)
        self.assertIn('value="31-02-2024"', response.text)

    def test_a_stored_date_is_shown_in_the_locale_it_has_now(self):
        form = self.series_form()
        form["name"] = "A series"
        form["startDate"] = "2024-03-15"
        self.assertEqual(self.save(form).status_int, 302)
        self.set_locale("en-US")
        page = self.web.get("/test_repo/series/1/edit", headers=ADMIN).text
        self.assertIn('value="03/15/2024"', page)


class LanguageFieldTest(DatesCase):
    def test_the_language_is_one_input_with_the_menu_of_languages_and_no_buttons(self):
        page = self.web.get("/test_repo/series/add", headers=ADMIN).text
        self.assertRegex(
            page,
            r'<input[^>]*name="inLanguage"[^>]*class="form-control language-code"',
        )
        self.assertNotIn("bag-inLanguage", page)
        self.assertIn('data-language="nl"', page)

    def test_a_language_is_a_code(self):
        form = self.series_form()
        form["name"] = "A series"
        form["inLanguage"] = "not a code"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)

    def test_the_language_is_stored(self):
        form = self.series_form()
        form["name"] = "A series"
        form["inLanguage"] = "nl"
        self.assertEqual(self.save(form).status_int, 302)
        series = self.application["test_repo"].types["series"]["1"]
        found = series.graph.objects(series.resource, "https://schema.org/inLanguage")
        self.assertEqual([o.value for o in found], ["nl"])


class CalendarTest(DatesCase):
    def test_a_date_has_a_calendar_menu_that_knows_the_locale(self):
        self.set_locale("nl-NL")
        page = self.web.get("/test_repo/series/add", headers=ADMIN).text
        self.assertIn('class="date-input"', page)
        self.assertIn('data-order="dmy" data-separator="-"', page)
        self.assertIn('data-locale="nl-NL" data-first-day="1"', page)
        self.assertIn('aria-label="Choose start date from a calendar"', page)
        self.assertIn('data-bs-toggle="dropdown"', page)
        self.assertIn("date.js", page)

    def test_a_year_alone_has_no_calendar(self):
        widget = forms.DateWidget(kinds=(dates.XSD_YEAR,))
        year = colander.SchemaNode(colander.String(), name="year", widget=widget)
        schema = colander.SchemaNode(colander.Mapping(), year)
        html = deform.Form(schema, renderer=forms.RENDERER).render({"year": "2024"})
        self.assertNotIn("date-pick", html)


class WorkFormTest(DatesCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "work_repo",
            admin=("admin", "correct horse battery"),
            content_types=[types.available()["work"]],
        )

    def form(self):
        response = self.web.get("/work_repo/work/add", headers=ADMIN)
        self.assertEqual(response.status_int, 200)
        form = response.forms[0]
        form["type"] = SCHEMA + "Thesis"
        return form

    def test_the_form_opens_with_the_date_after_the_names(self):
        names = [
            n for n in self.form().fields if n in ("name", "datePublished", "item")
        ]
        self.assertEqual(names[:2], ["name", "datePublished"])
        fields = [n for n in self.form().fields if n]
        self.assertLess(fields.index("lang"), fields.index("datePublished"))
        self.assertLess(fields.index("datePublished"), fields.index("inLanguage"))
        self.assertEqual(
            self.form()["datePublished"].attrs["placeholder"], "yyyy-mm-dd"
        )

    def bag_input(self, form, page, name):
        """The input of the first row of the bag of a property."""
        found = re.search(
            rf'(?s)id="(?:bag|list)-{name}".*?<input[^>]*name="item"[^>]*id="([^"]+)"',
            page,
        )
        assert found, name
        return next(f for f in form.fields["item"] if f.id == found[1])

    def test_a_publisher_and_a_parent_are_uris_too(self):
        response = self.web.get("/work_repo/work/add", headers=ADMIN)
        form, page = response.forms[0], response.text
        form["type"] = SCHEMA + "Thesis"
        form["name"] = "A work"
        self.bag_input(form, page, "author").value = "ark:/99999/ir/person/1"
        self.bag_input(form, page, "publisher").value = "ark:/99999/ir/organization/2"
        self.bag_input(form, page, "isPartOf").value = "ark:/99999/ir/series/3"
        response = form.submit("save", headers=POSTING, expect_errors=True)
        self.assertEqual(response.status_int, 302, response.text[:600])
        work = self.application["work_repo"].types["work"]["1"]
        for name, value in (
            ("publisher", "ark:/99999/ir/organization/2"),
            ("isPartOf", "ark:/99999/ir/series/3"),
        ):
            found = work.graph.objects(work.resource, f"https://schema.org/{name}")
            self.assertEqual(
                [(type(o).__name__, o.value) for o in found], [("NamedNode", value)]
            )
        self.assertEqual(work.validate(), [])

    def test_a_part_of_that_is_a_node_stays_when_the_references_are_saved(self):
        form = self.form()
        form["name"] = "An article"
        form.fields["item"][0].value = "ark:/99999/ir/person/1"
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)
        work = self.application["work_repo"].types["work"]["1"]
        part = "https://schema.org/isPartOf"
        issue = ox.BlankNode()
        work.graph.add_value(work.resource, part, issue)
        work.graph.add_value(
            issue, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "PublicationIssue")
        )
        values = work.form_values()
        values["resource"]["isPartOf"] = ["ark:/99999/ir/series/3"]
        work.apply_form(values)
        found = work.graph.objects(work.resource, part)
        self.assertEqual(
            sorted(type(o).__name__ for o in found), ["BlankNode", "NamedNode"]
        )
        self.assertEqual(
            work.graph.objects(issue, forms.RDF_TYPE),
            [ox.NamedNode(SCHEMA + "PublicationIssue")],
        )

    def test_an_author_that_is_no_uri_is_refused(self):
        form = self.form()
        form["name"] = "A work"
        form.fields["item"][0].value = "Ada Lovelace"
        response = form.submit("save", headers=POSTING, expect_errors=True)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a URI", response.text)


if __name__ == "__main__":
    unittest.main()
