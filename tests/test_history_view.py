import json
import re
import unittest
from datetime import datetime

from support import must_search
from test_source import SourceCase

from gracia.utils import pretty_modified

FOAF = "http://xmlns.com/foaf/0.1/"
ORIGIN = {"Origin": "http://localhost:80"}


class LastModifiedTest(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 12, 0, 0)

    def said(self, **ago) -> str:
        from datetime import timedelta

        return pretty_modified(self.NOW - timedelta(**ago), self.NOW)

    def test_how_long_ago_in_minutes_hours_or_days_and_after_30_days_the_day(self):
        self.assertEqual(self.said(seconds=20), "Last modified just now")
        self.assertEqual(self.said(minutes=1), "Last modified 1 minute ago")
        self.assertEqual(self.said(minutes=59), "Last modified 59 minutes ago")
        self.assertEqual(self.said(hours=1, minutes=5), "Last modified 1 hour ago")
        self.assertEqual(self.said(hours=23), "Last modified 23 hours ago")
        self.assertEqual(self.said(days=2), "Last modified 2 days ago")
        self.assertEqual(self.said(days=30), "Last modified 30 days ago")
        self.assertEqual(
            pretty_modified(datetime(2026, 8, 23, 9, 30), self.NOW),
            "Last modified on Sunday, August 23 2026",
        )
        self.assertEqual(
            pretty_modified(datetime(2026, 9, 7, 9, 30), datetime(2026, 11, 20)),
            "Last modified on Monday, September 7 2026",  # no zero before the day
        )


class HistoryCase(SourceCase):
    def versions(self, uri="rec:person:1"):
        with self.application.new_store("test_repo") as store:
            return store.revisions(uri)

    def rename(self, name: str) -> None:
        form = self.get("/test_repo/person/1/edit").forms[0]
        form["name"] = name
        self.assertEqual(form.submit("save", headers=self.admin).status_int, 302)

    def version(self, txn: int, uri="/test_repo/person/1/history") -> dict:
        got = self.get(
            f"{uri}?txn={txn}", headers={**self.admin, "Accept": "application/json"}
        )
        return json.loads(got.text)


class EditPageTest(HistoryCase):
    def test_it_says_when_the_record_was_last_changed_above_the_buttons_and_the_source_page_has_them_too(
        self,
    ):
        page = self.get("/test_repo/person/1/edit").text
        self.assertRegex(
            page,
            r'<div class="text-end text-muted small mb-1">Last modified (just now|\d+ '
            r"minutes? "
            r"ago)</div>",
        )
        self.assertLess(page.index("Last modified"), page.index(">History</a>"))
        self.assertRegex(
            page,
            r'<a class="btn btn-outline-secondary btn-sm" '
            r'href="http://localhost/test_repo/person/1/history">History</a>',
        )
        # the source page has them too, and a new record and a user have no history
        source = self.get("/test_repo/person/1/edit?source=turtle").text
        self.assertIn(">History</a>", source)
        self.assertIn(">Form Edit</a>", source)
        add = self.get("/test_repo/person/add").text
        self.assertNotIn(">History</a>", add)
        self.assertNotIn("Last modified", add)
        user = self.get("/test_repo/user/1/edit").text
        self.assertNotIn(
            ">History</a>", user
        )  # its history has the hash of the password
        self.assertEqual(self.status("/test_repo/user/1/history", self.admin), 404)


class HistoryPageTest(HistoryCase):
    def test_the_versions_newest_first_the_current_one_marked_and_restore_for_the_others(
        self,
    ):
        self.rename("Ada King")
        self.rename("Ada Countess")
        self.assertEqual(len(self.versions()), 3)
        page = self.get("/test_repo/person/1/history").text
        numbers = [int(n) for n in re.findall(r'id="revision-(\d+)"', page)]
        self.assertEqual(numbers, sorted(numbers, reverse=True))
        self.assertEqual(len(numbers), 3)
        newest = must_search(
            rf'id="revision-{numbers[0]}".*?</div>\s*</div>', page, re.S
        )[0]
        self.assertIn("current", newest)
        self.assertNotIn(">Restore</button>", newest)
        older = must_search(
            rf'id="revision-{numbers[1]}".*?</div>\s*</div>', page, re.S
        )[0]
        self.assertNotIn("current", older)
        self.assertIn(">Restore</button>", older)
        self.assertIn(
            f'data-history-view="http://localhost/test_repo/person/ada-king/history?txn={numbers[1]}"',
            older,
        )
        self.assertRegex(older, r'data-confirm="Restore version \d+\?')  # it asks first
        self.assertIn("history.js", page.split("</head>")[0])
        self.assertIn("manage.js", page.split("</head>")[0])
        self.assertIn(
            'data-editor="turtle"', page
        )  # the editor of the edit page, read only

    def test_a_version_is_its_text_and_what_it_changed_since_the_one_before_and_who_may_see_it(
        self,
    ):
        self.rename("Ada King")
        numbers = sorted(txn for txn, *_ in self.versions())
        old, new = self.version(numbers[0]), self.version(numbers[-1])
        self.assertIn('foaf:name "Ada"', old["source"])
        self.assertIn('foaf:name "Ada King"', new["source"])
        self.assertIsNone(old["diff"])  # the first version: nothing before it
        lines = new["diff"].splitlines()
        self.assertTrue(
            any(line.startswith("-") and '"Ada"' in line for line in lines), lines
        )
        self.assertTrue(
            any(line.startswith("+") and '"Ada King"' in line for line in lines), lines
        )
        self.assertEqual(
            self.status("/test_repo/person/1/history?txn=99999", self.admin), 404
        )
        self.assertEqual(
            self.status("/test_repo/person/1/history?txn=x", self.admin), 400
        )
        self.assertEqual(
            self.status("/test_repo/person/1/history"), 401
        )  # anonymous: log in
        self.assertEqual(self.status("/test_repo/person/1/history", self.editor), 200)


class RestoreTest(HistoryCase):
    def restore(self, txn, headers=None, **kw):
        return self.client().post(
            "/test_repo/person/1/restore",
            {"txn": str(txn)},
            headers=headers or {**self.admin, **ORIGIN},
            **kw,
        )

    def test_a_version_is_saved_as_the_new_version_and_the_history_keeps_all_of_them(
        self,
    ):
        original = self.values("rec:person:1")
        self.rename("Ada King")
        first = min(txn for txn, *_ in self.versions())
        self.assertIn((FOAF + "name", "Ada King"), self.values("rec:person:1"))
        web = self.client()  # one client, so that the cookie of the flash comes back
        got = web.post(
            "/test_repo/person/1/restore",
            {"txn": str(first)},
            headers={**self.admin, **ORIGIN},
        )
        self.assertEqual(got.status_int, 302)
        restored = self.values("rec:person:1")
        self.assertTrue(
            set(original) <= set(restored)
        )  # what it was, and what a save makes of it
        self.assertNotIn((FOAF + "name", "Ada King"), restored)
        self.assertIn(("https://gracia.pub/ns/label", "Ada"), restored)
        versions = self.versions()
        self.assertEqual(len(versions), 3)  # the restore is a version of its own
        self.assertIn(
            "Restored the version of",
            web.get(got.headers["Location"], headers=self.admin).text,
        )
        newest = self.version(versions[0][0])["source"]
        self.assertIn('foaf:name "Ada"', newest)  # the old name, in the newest version
        self.assertNotIn("Ada King", newest)
        self.assertIn(
            'foaf:name "Ada King"', self.version(versions[1][0])["source"]
        )  # kept

    def test_it_has_to_come_from_this_site_and_from_someone_who_may_edit_and_be_a_version_of_a_record_that_is_there(
        self,
    ):
        first = self.versions()[0][0]
        self.rename("Ada King")
        self.assertEqual(
            self.restore(first, headers=self.admin, expect_errors=True).status_int, 400
        )
        self.assertEqual(
            self.restore(first, headers={**ORIGIN}, expect_errors=True).status_int, 401
        )
        self.assertIn((FOAF + "name", "Ada King"), self.values("rec:person:1"))
        self.assertEqual(self.restore(99999, expect_errors=True).status_int, 404)
        with self.application.new_store("test_repo") as store:
            store.delete(["rec:person:1"])
        tombstone = self.versions()[0]
        self.assertTrue(tombstone[2])
        self.assertEqual(
            self.restore(tombstone[0], expect_errors=True).status_int, 410
        )  # gone, no longer unknown
