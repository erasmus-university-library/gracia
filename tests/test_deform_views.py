"""Deform views: a form calls a named view to change itself, declared next to the form."""

import re
from unittest import mock
from urllib.parse import urlencode

import colander
import deform
from deform_views_support import Colours
from pyramid.exceptions import ConfigurationConflictError
from test_web import HeaderPolicy, WebCase

from gracia.interfaces import IDeformView
from gracia.main import make_config
from gracia.views import deform as deform_views


class FieldsCase(WebCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository("library")
        self.settings["gracia.security_policy"] = HeaderPolicy()
        self.config = make_config(self.settings)
        self.config.scan("deform_views_support")
        self.config.commit()
        self.web = self.client(self.config)
        self.node = self.config.registry["application"]["library"]
        self.addCleanup(self.node.close)

    def form(self) -> deform.Form:
        request = mock.Mock(registry=self.config.registry)
        request.resource_url = lambda context, *name, query=None: (
            "http://localhost/library/" + "/".join(name) + (f"?{urlencode(query)}" if query else "")
        )
        schema = Colours().bind(request=request, context=self.node)
        return deform.Form(schema, buttons=())


def fields_rows(form, query):
    from urllib.parse import parse_qsl

    return deform_views.render_fields(form, parse_qsl(query))


class DeclaredTest(FieldsCase):
    def test_the_function_is_a_view_of_its_node_and_a_source_by_name(self):
        got = self.web.get("/library/deform/shades?family=red")
        self.assertIn("text/html", got.content_type)
        self.assertEqual(
            re.findall(r'<option value="([^"]*)">([^<]*)</option>', got.text),
            [("crimson", "Crimson"), ("scarlet", "Scarlet")],
        )
        self.assertNotIn("<select", got.text)
        from deform_views_support import shades

        self.assertIs(self.config.registry.queryUtility(IDeformView, "shades"), shades)

    def test_a_view_that_answers_a_string_is_html_as_it_is(self):
        got = self.web.get("/library/deform/palette?family=blue")
        self.assertIn("text/html", got.content_type)
        self.assertEqual(got.text, "<p id='palette'>blue</p>")

    def test_a_view_is_always_given_the_form_it_belongs_to(self):
        import deform_views_support as support

        support.SEEN.clear()
        form = self.form()
        form.render({})  # a widget calls the view while the form is rendered: with that very form
        self.assertIs(support.SEEN[-1][0], form)
        self.web.get("/library/deform/shades?family=red")  # over HTTP: a fresh form of that request
        self.assertIsInstance(support.SEEN[-1][0], deform.Form)
        self.assertIsNot(support.SEEN[-1][0], form)

    def test_a_view_is_given_the_target_field(self):
        import deform_views_support as support

        support.SEEN.clear()
        form = self.form()
        form.render({})  # the widget passes its own field
        self.assertIs(support.SEEN[-1][1], form["shade"])
        self.web.get("/library/deform/shades?field=shade&family=red")  # over HTTP: ?field=
        self.assertEqual(support.SEEN[-1][1].name, "shade")
        self.web.get("/library/deform/shades?family=red")
        self.assertIsNone(support.SEEN[-1][1])
        got = self.web.get("/library/deform/shades?field=nope&family=red", expect_errors=True)
        self.assertEqual(got.status_int, 404)

    def test_the_select_adds_its_own_name_to_its_url_so_one_view_can_serve_several(self):
        html = self.form().render({})
        self.assertIn('hx-get="http://localhost/library/deform/shades?field=shade"', html)

    def test_one_row_of_the_form_can_be_rendered_without_form_tags(self):
        got = self.web.get("/library/deform/row?field=family&family=blue&shade=navy").text
        self.assertRegex(
            got.lstrip(), r'^<div\s+class="mb-3\s+item-family"'
        )  # the row, nothing around
        self.assertIn('<option selected="selected" value="blue">', got)
        self.assertNotIn("<form", got)
        self.assertNotIn("<fieldset", got)
        self.assertNotIn("__formid__", got)
        self.assertNotIn('name="shade"', got)  # only the row that was asked for
        both = fields_rows(self.form(), "family=blue&shade=navy")
        self.assertIn('name="family"', both)
        self.assertIn('name="shade"', both)
        self.assertNotIn("<form", both)

    def test_a_deform_view_must_say_which_form_it_belongs_to(self):
        with self.assertRaises(TypeError):
            deform_views.deform_view("x", context=object)

    def test_the_views_live_in_one_namespace_so_they_cannot_clash_with_a_type(self):
        from gracia import uri

        self.assertIn("deform", uri.RESERVED_SLUGS)
        self.assertEqual(
            self.web.get("/library/shades?family=red", expect_errors=True).status_int, 404
        )
        self.assertEqual(self.web.get("/library/deform/nope", expect_errors=True).status_int, 404)
        self.assertEqual(
            self.web.get("/nolibrary/deform/shades", expect_errors=True).status_int, 404
        )

    def test_a_view_is_asked_for_its_permission_of_the_node_it_was_declared_for(self):
        import deform_views_support as support

        self.assertIsNone(support.shades.deform_at)  # the repository
        self.assertEqual(support.hue.deform_permission, "edit")
        from gracia.jobs.validate import schema_files

        self.assertEqual(
            (schema_files.deform_at, schema_files.deform_permission), ("jobs", "start_validate_job")
        )

    def test_a_view_can_answer_a_response_of_its_own(self):
        got = self.web.get("/library/deform/teapot", expect_errors=True)
        self.assertEqual((got.status_int, got.text), (418, "short and stout"))

    def test_a_view_that_returns_the_form_gets_a_fresh_one_and_answers_it_filled_in(self):
        user = {"X-User": "admin"}
        blue = self.web.get("/library/deform/hue?family=blue&shade=navy", headers=user).text
        self.assertIn('<input type="text" name="shade"', blue)  # the view changed the widget
        self.assertIn('value="navy"', blue)  # what was posted is in it
        self.assertIn('<option selected="selected" value="blue">', blue)
        self.assertNotIn("error", blue.lower())  # nothing is validated
        red = self.web.get("/library/deform/hue?family=red", headers=user).text
        self.assertIn('<select name="shade"', red)  # a fresh form: the blue change is not kept
        self.assertIn('value="crimson"', red)

    def test_a_view_declared_with_a_form_has_the_permission_it_was_declared_with(self):
        self.assertEqual(
            self.web.get("/library/deform/hue?family=red", expect_errors=True).status_int, 403
        )

    def test_the_view_has_the_permission_it_was_declared_with(self):
        self.assertEqual(self.web.get("/library/deform/shades?family=red").status_int, 200)

    def test_two_sources_with_one_name_are_a_conflict(self):
        config = make_config(self.settings)
        config.scan("deform_views_support")
        config.add_gracia_deform_view("shades", lambda context, request, values, form, field: [])
        with self.assertRaises(ConfigurationConflictError):
            config.commit()


class WidgetTest(FieldsCase):
    def test_the_dependent_select_declares_that_it_follows_the_other_one(self):
        html = self.form().render({})
        family = re.search(r'<select name="family"[^>]*>', html)[0]
        shade = re.search(r'<select name="shade"[^>]*>', html)[0]
        self.assertNotIn("hx-", family)
        family_id = re.search(r'id="([^"]+)"', family)[1]
        self.assertIn('hx-get="http://localhost/library/deform/shades?field=shade"', shade)
        self.assertIn(f'hx-trigger="change from:#{family_id}"', shade)
        self.assertIn(f'hx-include="#{family_id}"', shade)
        self.assertIn('hx-target="this"', shade)
        self.assertIn('hx-swap="innerHTML"', shade)

    def test_the_first_time_it_has_the_choices_for_what_the_other_select_shows(self):
        html = self.form().render({})
        self.assertIn('value="crimson"', html)  # red is the first family
        self.assertNotIn("navy", html)

    def test_a_form_that_comes_back_has_the_choices_for_what_was_posted(self):
        form = self.form()
        with self.assertRaises(deform.ValidationFailure) as caught:
            form.validate([("family", "blue"), ("shade", "crimson")])
        html = caught.exception.render()
        self.assertIn('value="navy"', html)
        self.assertNotIn('value="crimson"', html.split('<select name="shade"')[1])

    def test_an_unknown_deform_view_fails_when_the_form_is_made(self):
        class Wrong(colander.MappingSchema):
            family = colander.SchemaNode(colander.String())
            shade = colander.SchemaNode(
                colander.String(), widget=deform_views.dependent_select("nope", depends_on="family")
            )

        request = mock.Mock(registry=self.config.registry)
        with self.assertRaises(LookupError) as caught:
            Wrong().bind(request=request, context=self.node)
        self.assertIn("@deform_view('nope'", str(caught.exception))


class ValidatorTest(FieldsCase):
    def validate(self, family, shade):
        return self.form().validate([("family", family), ("shade", shade)])

    def test_a_value_the_source_offers_for_the_other_value_is_accepted(self):
        self.assertEqual(self.validate("blue", "navy"), {"family": "blue", "shade": "navy"})

    def test_a_value_it_does_not_offer_for_it_is_refused_on_the_server(self):
        with self.assertRaises(deform.ValidationFailure) as caught:
            self.validate("blue", "crimson")  # a shade of red
        self.assertIn("Not one of the choices for this family", caught.exception.render())
