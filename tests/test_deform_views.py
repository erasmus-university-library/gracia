import re
from unittest import mock
from urllib.parse import urlencode

import colander
import deform
from deform_views_support import Colours
from pyramid.exceptions import ConfigurationConflictError
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import templates
from gracia.interfaces import IDeformView
from gracia.main import make_config


class FieldsCase(WebCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository("test_repo")
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.scan("deform_views_support")
        self.config.commit()
        self.web = self.client(self.config)
        self.node = self.config.registry["application"]["test_repo"]  # ty: ignore[not-subscriptable]
        self.addCleanup(self.node.close)

    def form(self) -> deform.Form:
        request = mock.Mock(registry=self.config.registry)
        request.resource_url = lambda context, *name, query=None: (
            "http://localhost/test_repo/"
            + "/".join(name)
            + (f"?{urlencode(query)}" if query else "")
        )
        schema = Colours().bind(request=request, context=self.node)
        return deform.Form(schema, buttons=())


def fields_rows(form, query):
    from urllib.parse import parse_qsl

    return templates.DeformViews.render_fields(form, parse_qsl(query))


class DeclaredTest(FieldsCase):
    def test_a_view_is_a_view_of_its_node_and_a_source_by_name_and_answers_html_or_a_response_of_its_own(
        self,
    ):
        # the function is a view of its node and a source by name
        got = self.web.get("/test_repo/deform/shades?family=red")
        self.assertIn("text/html", got.content_type)
        self.assertEqual(
            re.findall(r'<option value="([^"]*)">([^<]*)</option>', got.text),
            [("crimson", "Crimson"), ("scarlet", "Scarlet")],
        )
        self.assertNotIn("<select", got.text)
        from deform_views_support import shades

        self.assertIs(self.config.registry.queryUtility(IDeformView, "shades"), shades)  # ty: ignore[missing-union-attribute]

        # a view that answers a string is html as it is
        got = self.web.get("/test_repo/deform/palette?family=blue")
        self.assertIn("text/html", got.content_type)
        self.assertEqual(got.text, "<p id='palette'>blue</p>")

        # a view can answer a response of its own
        got = self.web.get("/test_repo/deform/teapot", expect_errors=True)
        self.assertEqual((got.status_int, got.text), (418, "short and stout"))

        # the views live in one namespace so they cannot clash with a type
        from gracia import uri

        self.assertIn("deform", uri.RESERVED_SLUGS)
        self.assertEqual(
            self.web.get("/test_repo/shades?family=red", expect_errors=True).status_int,
            404,
        )
        self.assertEqual(
            self.web.get("/test_repo/deform/nope", expect_errors=True).status_int, 404
        )
        self.assertEqual(
            self.web.get("/nolibrary/deform/shades", expect_errors=True).status_int, 404
        )

    def test_a_view_is_given_its_form_and_the_target_field_and_the_select_adds_its_own_name_to_its_url(
        self,
    ):
        # a view is always given the form it belongs to
        import deform_views_support as support

        support.SEEN.clear()
        form = self.form()
        form.render(
            {}
        )  # a widget calls the view while the form is rendered: with that very form
        self.assertIs(support.SEEN[-1][0], form)
        self.web.get(
            "/test_repo/deform/shades?family=red"
        )  # over HTTP: a fresh form of that request
        self.assertIsInstance(support.SEEN[-1][0], deform.Form)
        self.assertIsNot(support.SEEN[-1][0], form)

        # a view is given the target field
        import deform_views_support as support

        support.SEEN.clear()
        form = self.form()
        form.render({})  # the widget passes its own field
        self.assertIs(support.SEEN[-1][1], form["shade"])
        self.web.get(
            "/test_repo/deform/shades?field=shade&family=red"
        )  # over HTTP: ?field=
        self.assertEqual(support.SEEN[-1][1].name, "shade")
        self.web.get("/test_repo/deform/shades?family=red")
        self.assertIsNone(support.SEEN[-1][1])
        got = self.web.get(
            "/test_repo/deform/shades?field=nope&family=red", expect_errors=True
        )
        self.assertEqual(got.status_int, 404)

        # the select adds its own name to its url so one view can serve several
        html = self.form().render({})
        self.assertIn(
            'hx-get="http://localhost/test_repo/deform/shades?field=shade"', html
        )

    def test_one_row_of_the_form_can_be_rendered_without_form_tags_and_a_view_can_return_the_form(
        self,
    ):
        # one row of the form can be rendered without form tags
        got = self.web.get(
            "/test_repo/deform/row?field=family&family=blue&shade=navy"
        ).text
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

        # a view that returns the form gets a fresh one and answers it filled in
        user = {"X-User": "admin"}
        blue = self.web.get(
            "/test_repo/deform/hue?family=blue&shade=navy", headers=user
        ).text
        self.assertIn(
            '<input type="text" name="shade"', blue
        )  # the view changed the widget
        self.assertIn('value="navy"', blue)  # what was posted is in it
        self.assertIn('<option selected="selected" value="blue">', blue)
        self.assertNotIn("error", blue.lower())  # nothing is validated
        red = self.web.get("/test_repo/deform/hue?family=red", headers=user).text
        self.assertIn(
            '<select name="shade"', red
        )  # a fresh form: the blue change is not kept
        self.assertIn('value="crimson"', red)

    def test_a_deform_view_must_say_which_form_it_belongs_to_and_two_sources_with_one_name_conflict(
        self,
    ):
        # a deform view must say which form it belongs to
        with self.assertRaises(TypeError):
            templates.DeformViews.declare("x", context=object)  # ty: ignore[missing-argument, unknown-argument]

        # two sources with one name are a conflict
        config = make_config(self.settings)
        config.scan("deform_views_support")
        config.add_gracia_deform_view(
            "shades", lambda context, request, values, form, field: []
        )
        with self.assertRaises(ConfigurationConflictError):
            config.commit()

    def test_a_view_has_the_permission_it_was_declared_with_for_the_node_it_was_declared_for(
        self,
    ):
        # a view is asked for its permission of the node it was declared for
        import deform_views_support as support

        self.assertIsNone(support.shades.deform_at)  # the repository
        self.assertEqual(support.hue.deform_permission, "edit")
        from gracia.jobs.validate import schema_files

        self.assertEqual(
            (schema_files.deform_at, schema_files.deform_permission),
            ("jobs", "start_validate_job"),
        )

        # a view declared with a form has the permission it was declared with
        self.assertEqual(
            self.web.get(
                "/test_repo/deform/hue?family=red", expect_errors=True
            ).status_int,
            403,
        )

        # the view has the permission it was declared with
        self.assertEqual(
            self.web.get("/test_repo/deform/shades?family=red").status_int, 200
        )


class WidgetTest(FieldsCase):
    def test_the_dependent_select_follows_the_other_one_and_has_the_choices_for_what_it_shows_or_was_posted(
        self,
    ):
        # the dependent select declares that it follows the other one
        html = self.form().render({})
        family = must_search(r'<select name="family"[^>]*>', html)[0]
        shade = must_search(r'<select name="shade"[^>]*>', html)[0]
        self.assertNotIn("hx-", family)
        family_id = must_search(r'id="([^"]+)"', family)[1]
        self.assertIn(
            'hx-get="http://localhost/test_repo/deform/shades?field=shade"', shade
        )
        self.assertIn(f'hx-trigger="change from:#{family_id}"', shade)
        self.assertIn(f'hx-include="#{family_id}"', shade)
        self.assertIn('hx-target="this"', shade)
        self.assertIn('hx-swap="innerHTML"', shade)

        # the first time it has the choices for what the other select shows
        html = self.form().render({})
        self.assertIn('value="crimson"', html)  # red is the first family
        self.assertNotIn("navy", html)

        # a form that comes back has the choices for what was posted
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
                colander.String(),
                widget=templates.DependentSelectWidget.deferred(
                    "nope", depends_on="family"
                ),
            )

        request = mock.Mock(registry=self.config.registry)
        with self.assertRaises(LookupError) as caught:
            Wrong().bind(request=request, context=self.node)
        self.assertIn("@deform_view('nope'", str(caught.exception))


class ValidatorTest(FieldsCase):
    def validate(self, family, shade):
        return self.form().validate([("family", family), ("shade", shade)])

    def test_a_value_the_source_offers_is_accepted_and_one_it_does_not_offer_is_refused_on_the_server(
        self,
    ):
        # a value the source offers for the other value is accepted
        self.assertEqual(
            self.validate("blue", "navy"), {"family": "blue", "shade": "navy"}
        )

        # a value it does not offer for it is refused on the server
        with self.assertRaises(deform.ValidationFailure) as caught:
            self.validate("blue", "crimson")  # a shade of red
        self.assertIn(
            "Not one of the choices for this family", caught.exception.render()
        )
