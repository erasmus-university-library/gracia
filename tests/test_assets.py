import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyoxigraph as ox
from base import DbTestCase
from pyramid.config import Configurator
from pyramid.threadlocal import manager
from test_web import WebCase

from gracia import cli, init, uri
from gracia.utils import assets

SHAPE = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
<urn:test:Shape> a sh:NodeShape ; sh:targetClass ex:Thing ;
    sh:property [ sh:path ex:label ; sh:name "@NAME@" ; sh:maxCount 1 ] .
"""
THING = "http://example.org/Thing"


class JoinTest(unittest.TestCase):
    def test_a_name_stays_inside_the_directory(self):
        self.assertEqual(
            assets.join("gracia.types:user/", "view.xsl"),
            "gracia.types:user/view.xsl",
        )
        self.assertEqual(
            assets.join("gracia.types:user", "a/b.xsl"),
            "gracia.types:user/a/b.xsl",
        )
        self.assertEqual(assets.join("pkg:", "x.ttl"), "pkg:x.ttl")
        for bad in ("", "/etc/passwd", "../x", "a/../../x", "a//b", "./x", "a\\b"):
            with self.assertRaises(ValueError, msg=bad):
                assets.join("gracia.types:user/", bad)


class SchemaNameTest(unittest.TestCase):
    """`<Type>-<profile>-<revision>.ttl`, of the type that asks for it."""

    def test_a_schema_file_of_the_type(self):
        for name in (
            "Person-full-03.ttl",
            "Person-minimal-01.ttl",
            "Person-short_form-120.ttl",
        ):
            self.assertEqual(
                assets.schema_spec("gracia.types:person/", name, "Person"),
                f"gracia.types:person/shacl/{name}",
            )

    def test_anything_else_is_refused(self):
        for name in (
            None,
            "",
            "Person-full-3.ttl",  # the revision has two digits at least
            "Person-Full-03.ttl",  # a profile is lower case
            "Person-full.ttl",
            "Person.ttl",
            "person-full-03.ttl",  # not the name of this type
            "Work-full-03.ttl",  # the file of another type
            "Person-full-03.xsl",
            "../Person-full-03.ttl",
            "shacl/Person-full-03.ttl",
            "Person-full-03.ttl/../x",
        ):
            with self.assertRaises(ValueError, msg=name):
                assets.schema_spec("gracia.types:person/", name, "Person")


class ShapeLoadingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "shacl").mkdir()

    def write(self, name="Thing-full-01.ttl", label="First"):
        path = self.dir / "shacl" / name
        path.write_text(SHAPE.replace("@NAME@", label))
        return path

    def shape(self, schema="Thing-full-01.ttl", class_uri=THING):
        return assets.shape(f"{self.dir}/", schema, class_uri, "Thing")

    def test_the_shape_of_a_class_is_found_by_its_target_class(self):
        self.write()
        data, node = self.shape()
        self.assertEqual(node, ox.NamedNode("urn:test:Shape"))
        self.assertGreater(len(data), 0)

    def test_the_types_that_come_with_gracia_have_their_schemas_in_their_packages(self):
        for slug, name, class_uri in (
            ("repository", "Repository", uri.REPOSITORY_CLASS),
            ("type", "Type", uri.TYPE_CLASS),
            ("user", "User", uri.USER_CLASS),
            ("group", "UserGroup", uri.GROUP_CLASS),
        ):
            prefix = init.TYPE_ASSETS.format(slug=slug)
            self.assertIsNotNone(
                assets.shape(prefix, f"{name}-full-01.ttl", class_uri, name), slug
            )

    def test_a_missing_or_wrong_shape_is_none_not_an_error(self):
        self.assertIsNone(assets.shape(None, "Thing-full-01.ttl", THING, "Thing"))
        self.assertIsNone(
            assets.shape(f"{self.dir}/", None, THING, "Thing")
        )  # no schema named
        self.assertIsNone(
            self.shape()
        )  # the file is not there (not even logged as an error)
        self.write()
        with self.assertLogs("gracia.utils.assets", "WARNING"):
            self.assertIsNone(self.shape(class_uri="http://example.org/Other"))
        with self.assertLogs("gracia.utils.assets", "WARNING"):
            self.assertIsNone(
                self.shape(schema="Other-full-01.ttl")
            )  # another type's name
        self.write().write_text("this is not turtle")
        with self.assertLogs("gracia.utils.assets", "WARNING"):
            self.assertIsNone(self.shape())
        with self.assertLogs("gracia.utils.assets", "WARNING"):
            self.assertIsNone(
                assets.shape("no.such.package:x/", "Thing-full-01.ttl", THING, "Thing")
            )

    def test_a_changed_file_is_read_again_and_an_unchanged_one_is_not_parsed_twice(
        self,
    ):
        path = self.write(label="First")
        first = self.shape()
        self.assertIs(self.shape(), first)  # cached
        self.write(label="Second")
        os.utime(path, ns=(10**18, 10**18))  # a different modification time
        second = self.shape()
        self.assertIsNot(second, first)
        names = [
            q.object.value for q in second[0] if q.predicate.value.endswith("#name")
        ]
        self.assertEqual(names, ["Second"])

    def test_a_directory_is_not_an_asset(self):
        with self.assertRaises(FileNotFoundError):
            assets.resolve(str(self.dir))


class ProfilesTest(unittest.TestCase):
    """One type, two profiles and a new revision: each is a file, the type picks one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "shacl").mkdir()
        for name, label in (
            ("Thing-full-01.ttl", "Full, first revision"),
            ("Thing-full-02.ttl", "Full, second revision"),
            ("Thing-minimal-01.ttl", "Minimal"),
        ):
            (self.dir / "shacl" / name).write_text(SHAPE.replace("@NAME@", label))

    def label(self, schema):
        data, _ = assets.shape(f"{self.dir}/", schema, THING, "Thing")
        return next(q.object.value for q in data if q.predicate.value.endswith("#name"))

    def test_the_schema_a_type_names_is_the_one_it_uses(self):
        self.assertEqual(self.label("Thing-full-01.ttl"), "Full, first revision")
        self.assertEqual(self.label("Thing-full-02.ttl"), "Full, second revision")
        self.assertEqual(self.label("Thing-minimal-01.ttl"), "Minimal")

    def test_switching_schema_changes_the_version_of_the_type(self):
        prefix = f"{self.dir}/"
        versions = {
            name: assets.version(prefix, name, "Thing")
            for name in (
                "Thing-full-01.ttl",
                "Thing-full-02.ttl",
                "Thing-minimal-01.ttl",
            )
        }
        self.assertEqual(len(set(versions.values())), 3)
        # the others files of the directory do not count: only the one the type is on
        (self.dir / "shacl" / "Thing-full-03.ttl").write_text(
            SHAPE.replace("@NAME@", "New")
        )
        self.assertEqual(
            assets.version(prefix, "Thing-full-01.ttl", "Thing"),
            versions["Thing-full-01.ttl"],
        )
        self.assertNotEqual(
            assets.version(prefix, "Thing-full-03.ttl", "Thing"),
            versions["Thing-full-01.ttl"],
        )

    def test_a_schema_that_does_not_exist_leaves_only_the_stylesheet(self):
        prefix = f"{self.dir}/"
        self.assertEqual(assets.version(prefix, "Thing-full-09.ttl", "Thing"), "")
        (self.dir / "view.xsl").write_text("view")
        only_view = assets.version(prefix, "Thing-full-09.ttl", "Thing")
        self.assertNotEqual(only_view, "")
        self.assertEqual(
            assets.version(prefix, "Other-full-01.ttl", "Thing"), only_view
        )  # invalid


class OverrideTest(unittest.TestCase):
    """`config.override_asset` layers a directory over a type's files, per registry."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for package in ("base_types", "custom_types"):
            (root / package / "thing" / "shacl").mkdir(parents=True)
            (root / package / "__init__.py").touch()
        schema = "shacl/Thing-full-01.ttl"
        (root / "base_types/thing" / schema).write_text(SHAPE.replace("@NAME@", "Base"))
        (root / "base_types/thing/view.xsl").write_text("base view")
        (root / "custom_types/thing" / schema).write_text(
            SHAPE.replace("@NAME@", "Custom")
        )
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(
            lambda: [sys.modules.pop(m, None) for m in ("base_types", "custom_types")]
        )

    def label(self):
        data, _ = assets.shape("base_types:thing/", "Thing-full-01.ttl", THING, "Thing")
        return next(q.object.value for q in data if q.predicate.value.endswith("#name"))

    def test_the_original_without_an_override(self):
        self.assertEqual(self.label(), "Base")

    def test_an_override_replaces_a_file_and_the_rest_falls_through(self):
        config = Configurator()
        config.override_asset("base_types:thing/", "custom_types:thing/")
        config.commit()
        manager.push({"registry": config.registry, "request": None})
        self.addCleanup(manager.pop)
        self.assertEqual(self.label(), "Custom")
        # the stylesheet is not in the custom directory: the original is used
        self.assertEqual(assets.read_text("base_types:thing/view.xsl"), "base view")
        # another application in the same process is not affected
        other = Configurator()
        other.commit()
        manager.push({"registry": other.registry, "request": None})
        try:
            self.assertEqual(self.label(), "Base")
        finally:
            manager.pop()


class InfoTest(unittest.TestCase):
    """What the application knows about a file, computed in the process from the
    file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_checksum_size_and_media_type_of_a_file(self):
        (self.dir / "shape.ttl").write_text("<a> <b> <c> .")
        item = assets.info(f"{self.dir}/shape.ttl")
        self.assertEqual(
            item.checksum, "md5:" + hashlib.md5(b"<a> <b> <c> .").hexdigest()
        )
        self.assertEqual((item.size, item.media_type), (13, "text/turtle"))
        (self.dir / "view.xsl").write_text("x")
        self.assertEqual(
            assets.info(f"{self.dir}/view.xsl").media_type, "application/xslt+xml"
        )
        (self.dir / "other.bin").write_text("x")
        self.assertEqual(
            assets.info(f"{self.dir}/other.bin").media_type, "application/octet-stream"
        )

    def test_the_checksum_follows_the_file_and_is_not_computed_twice_for_one_version(
        self,
    ):
        path = self.dir / "shape.ttl"
        path.write_text("one")
        first = assets.info(f"{self.dir}/shape.ttl")
        with mock.patch.object(
            assets, "_checksum", side_effect=AssertionError("computed again")
        ):
            self.assertEqual(assets.info(f"{self.dir}/shape.ttl"), first)  # cached
        path.write_text("two!")
        second = assets.info(f"{self.dir}/shape.ttl")
        self.assertNotEqual(second.checksum, first.checksum)
        self.assertEqual(second.size, 4)

    def test_a_directory_has_one_version_that_follows_every_file(self):
        prefix, schema = f"{self.dir}/", "Thing-full-01.ttl"
        (self.dir / "shacl").mkdir()

        def version():
            return assets.version(prefix, schema, "Thing")

        self.assertEqual(assets.version(None), "")
        self.assertEqual(version(), "")  # no files
        (self.dir / "shacl" / schema).write_text("shape")
        only_shape = version()
        self.assertEqual(len(only_shape), 32)
        (self.dir / "view.xsl").write_text("view")
        both = version()
        self.assertNotEqual(both, only_shape)  # a file was added
        (self.dir / "view.xsl").write_text("view, changed")
        self.assertNotEqual(version(), both)  # a file changed
        (self.dir / "view.xsl").write_text("view")
        self.assertEqual(version(), both)  # and back again
        (self.dir / "view.xsl").unlink()
        self.assertEqual(version(), only_shape)  # a file went

    def test_files_that_are_not_declared_are_not_part_of_it(self):
        (self.dir / "view.xsl").write_text("view")
        before = assets.version(f"{self.dir}/")
        (self.dir / "notes.txt").write_text("anything")
        self.assertEqual(assets.version(f"{self.dir}/"), before)

    def test_the_version_is_that_of_the_overriding_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for package in ("vbase", "vcustom"):
            (root / package / "type").mkdir(parents=True)
            (root / package / "__init__.py").touch()
        (root / "vbase/type/view.xsl").write_text("base")
        (root / "vcustom/type/view.xsl").write_text("custom")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(
            lambda: [sys.modules.pop(m, None) for m in ("vbase", "vcustom")]
        )
        plain = assets.version("vbase:type/")
        config = Configurator()
        config.override_asset("vbase:type/", "vcustom:type/")
        config.commit()
        with assets.using(config.registry):
            overridden = assets.version("vbase:type/")
            self.assertEqual(
                assets.info("vbase:type/view.xsl").checksum,
                "md5:" + hashlib.md5(b"custom").hexdigest(),
            )
        self.assertNotEqual(overridden, plain)
        self.assertEqual(assets.version("vbase:type/"), plain)  # outside: no overrides


class TypeVersionTest(WebCase):
    def test_a_type_exposes_the_version_of_its_files(self):
        self.application.create_repository("test_repo")
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        self.assertEqual(
            node["user"].assets_version,
            assets.version("gracia.types:user/", "User-full-01.ttl", "User"),
        )
        self.assertNotEqual(node["user"].assets_version, node["group"].assets_version)
        self.assertEqual(len(node["user"].assets_version), 32)


class ListCommandTest(DbTestCase):
    def test_it_lists_each_file_the_types_use_once(self):
        self.create_repositories("test_repo", "archive")
        code, out, err = self.gracia("assets", "list")
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)  # the four system types: the same files, once
        for spec in (
            "gracia.types:user/shacl/User-full-01.ttl",
            "gracia.types:group/shacl/UserGroup-full-01.ttl",
            "gracia.types:type/shacl/Type-full-01.ttl",
            "gracia.types:repository/shacl/Repository-full-01.ttl",
        ):
            line = next(entry for entry in lines if entry.endswith(spec))
            self.assertRegex(line, r"^md5:[0-9a-f]{32}  +\d+  text/turtle +")

    def test_a_named_repository_that_does_not_exist_is_an_error(self):
        self.create_repositories("test_repo")
        code, _, err = self.gracia("assets", "list", "nope")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_the_old_sync_command_is_gone(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["assets", "sync"])
