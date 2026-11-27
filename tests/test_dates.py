"""Dates in the forms, as the locale of the repository (`gracia:locale`) writes them."""

import re
import unittest

import colander
import deform
from widgets_support import ADMIN, POSTING, XSD, FormCase

from gracia import forms
from gracia.utils import dates

STARTED = "started"


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
        # a month and a year alone are dates too
        self.assertEqual(dates.display("2024-03", "nl-NL"), "03-2024")
        self.assertEqual(dates.display("2024-03", "en-US"), "03/2024")
        self.assertEqual(dates.display("2024-03", "ja-JP"), "2024/03")
        self.assertEqual(dates.display("2024", "nl-NL"), "2024")
        self.assertEqual(dates.parse("3-2024", "nl-NL"), "2024-03")
        self.assertEqual(dates.parse("2024", "en-US"), "2024")
        # ISO is understood whatever the locale, and single digits are filled in
        self.assertEqual(dates.parse("2024-03-15", "en-US"), "2024-03-15")
        self.assertEqual(dates.parse("2024-03", "nl-NL"), "2024-03")
        self.assertEqual(dates.parse("5-3-2024", "nl-NL"), "2024-03-05")
        self.assertEqual(dates.parse(" 3/5/2024 ", "en-US"), "2024-03-05")

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

    def test_the_kinds_a_shape_allows_are_what_the_text_the_datatype_and_the_hint_say(
        self,
    ):
        self.assertEqual(dates.datatype_of("2024-03-15"), XSD + "date")
        self.assertEqual(dates.datatype_of("2024-03"), XSD + "gYearMonth")
        self.assertEqual(dates.datatype_of("2024"), XSD + "gYear")
        self.assertIsNone(dates.datatype_of("15-03-2024"))
        self.assertIsNone(dates.datatype_of(""))
        with self.assertRaises(ValueError):
            dates.parse("2024", "nl-NL", (dates.XSD_DATE,))
        self.assertEqual(
            dates.parse("15-03-2024", "nl-NL", (dates.XSD_DATE,)), "2024-03-15"
        )
        self.assertEqual(dates.placeholder("nl-NL"), "dd-mm-yyyy")
        self.assertEqual(dates.placeholder("en-US"), "mm/dd/yyyy")
        self.assertEqual(dates.placeholder("", (dates.XSD_YEAR,)), "yyyy")


class DatesCase(FormCase):
    def settings_form(self):
        return self.web.get("/test_repo/manage?tab=settings", headers=ADMIN).forms[
            "form-repository-settings"
        ]

    def set_locale(self, code):
        form = self.settings_form()
        form["locale"] = code
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def gadget_form(self, path="/test_repo/gadget/add"):
        form = self.web.get(path, headers=ADMIN).forms[0]
        form["name"] = "A gadget"
        return form

    def save(self, form):
        return form.submit("save", headers=POSTING, expect_errors=True)


class LocaleSettingTest(DatesCase):
    def test_the_settings_offer_the_formats_and_keep_the_one_that_is_chosen(self):
        page = self.page("/test_repo/manage?tab=settings")
        select = re.search(r'(?s)<select[^>]*name="locale".*?</select>', page)
        assert select is not None
        self.assertIn('selected value="">(none: ISO 8601)', select[0])
        self.assertIn("Dutch (15-03-2024)", select[0])
        self.assertEqual(self.application["test_repo"].locale(), "")
        self.set_locale("nl-NL")
        self.assertEqual(self.application["test_repo"].locale(), "nl-NL")
        form = self.settings_form()  # a name that is none of them is refused
        form["locale"].force_value("xx-XX")
        self.assertEqual(self.save(form).status_int, 200)
        self.assertEqual(self.application["test_repo"].locale(), "nl-NL")


class DateFormTest(DatesCase):
    def test_a_date_is_typed_as_the_locale_writes_it_and_stored_as_a_typed_iso_date(
        self,
    ):
        self.set_locale("nl-NL")
        form = self.gadget_form()
        self.assertEqual(form[STARTED].attrs.get("placeholder"), "dd-mm-yyyy")
        form[STARTED] = "15-03-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 302, response.text[:600])
        found = self.values(STARTED)
        self.assertEqual(
            [(o.value, o.datatype.value) for o in found], [("2024-03-15", XSD + "date")]
        )
        self.assertIn('value="15-03-2024"', self.page("/test_repo/gadget/1/edit"))
        # the date is shown in the locale the repository has now
        self.set_locale("en-US")
        self.assertIn('value="03/15/2024"', self.page("/test_repo/gadget/1/edit"))
        # a year is stored as a year
        self.set_locale("nl-NL")
        form = self.gadget_form()
        form[STARTED] = "2024"
        self.assertEqual(self.save(form).status_int, 302)
        found = self.values(STARTED, number=2)
        self.assertEqual(
            [(o.value, o.datatype.value) for o in found], [("2024", XSD + "gYear")]
        )

    def test_without_a_locale_dates_are_iso(self):
        form = self.gadget_form()
        self.assertEqual(form[STARTED].attrs.get("placeholder"), "yyyy-mm-dd")
        form[STARTED] = "2024-03-15"
        self.assertEqual(self.save(form).status_int, 302)

    def test_what_is_not_a_date_or_not_a_kind_of_date_the_shape_allows_is_refused(self):
        self.set_locale("nl-NL")
        form = self.gadget_form()
        form[STARTED] = "31-02-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a date that exists", response.text)
        self.assertIn('value="31-02-2024"', response.text)  # it stays in the input
        form = self.gadget_form()  # the shape wants a date or a year, not a month
        form[STARTED] = "03-2024"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Give it as", response.text)


class LanguageFieldTest(DatesCase):
    def test_the_language_is_one_input_with_the_menu_of_languages_and_is_a_code(self):
        page = self.page("/test_repo/gadget/add")
        self.assertRegex(
            page,
            r'<input[^>]*name="language"[^>]*class="form-control language-code"',
        )
        self.assertNotIn("bag-language", page)  # and no buttons
        self.assertIn('data-language="nl"', page)
        form = self.gadget_form()
        form["language"] = "not a code"
        response = self.save(form)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)
        form = self.gadget_form()
        form["language"] = "nl"
        self.assertEqual(self.save(form).status_int, 302)
        self.assertEqual([o.value for o in self.values("language")], ["nl"])


class CalendarTest(DatesCase):
    def test_a_date_has_a_calendar_menu_that_knows_the_locale_and_a_year_alone_has_none(
        self,
    ):
        self.set_locale("nl-NL")
        page = self.page("/test_repo/gadget/add")
        self.assertIn('class="date-input"', page)
        self.assertIn('data-order="dmy" data-separator="-"', page)
        self.assertIn('data-locale="nl-NL" data-first-day="1"', page)
        self.assertIn('aria-label="Choose start date from a calendar"', page)
        self.assertIn('data-bs-toggle="dropdown"', page)
        self.assertIn("date.js", page)
        widget = forms.DateWidget(kinds=(dates.XSD_YEAR,))
        year = colander.SchemaNode(colander.String(), name="year", widget=widget)
        schema = colander.SchemaNode(colander.Mapping(), year)
        html = deform.Form(schema, renderer=forms.RENDERER).render({"year": "2024"})
        self.assertNotIn("date-pick", html)


if __name__ == "__main__":
    unittest.main()
