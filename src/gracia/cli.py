import argparse
import contextlib
import datetime
import getpass
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

import duckdb

from gracia import config, types
from gracia.application import Application
from gracia.graph import (
    INPUT_BY_EXTENSION,
    JSON_TRIPLES,
    OUTPUT_FORMATS,
    RdfError,
    guess_format,
    streaming_parse,
    streaming_serialize,
)
from gracia.resources import Repository
from gracia.store import (
    DEFAULT_PORT,
    DEFAULT_REPOSITORY,
    NoSuchRepository,
    Store,
    read_owner,
    serve_db,
)
from gracia.utils import assets, passwords, xslt
from gracia.utils.validate import validate_password, validate_userid

_probe_error = ""  # why the last owner discovery failed, for the lock-error hint


def _discover_owner(
    settings: dict[str, str], port: int = DEFAULT_PORT
) -> dict[str, str]:
    """When the database is a file and an owner serves it, use the owner: opening the
    file would hit its lock. The owner says where it listens in `<db>.owner` (else the
    default local port is tried when a `<db>.token` is next to the file); connecting
    confirms it. Without an owner nothing changes."""
    global _probe_error
    target = settings["gracia.db"]
    if config.is_remote(target) or target == ":memory:":
        return settings
    found = read_owner(target)
    uri = found[0] if found else f"quack:localhost:{port}"
    token_file = Path(
        settings.get("gracia.db.token_file")
        or (found[1] if found else f"{target}.token")
    )
    if not token_file.is_file():
        return settings
    try:
        token = config.read_token({"gracia.db.token_file": str(token_file)})
        Application(uri, token=token).close()  # connecting is the probe
    except ValueError as err:
        _probe_error = (
            f"an owner was expected at {uri} (token file {token_file}): {err}"
        )
        return settings
    return {
        **settings,
        "gracia.db": uri,
        "gracia.db.token_file": str(token_file),
        "gracia.db.file": settings.get("gracia.db.file") or target,
    }


def _config_path(args) -> str | None:
    """The config file in use, as an absolute path: what a job's process is started
    with, so that it reads the same file whatever directory it ends up in."""
    found = config.config_file(args.config)
    return str(found.resolve()) if found else None


def _settings(args) -> dict[str, str]:
    return _discover_owner(config.resolve(args.config, args.db))


def _application(args) -> Application:
    """The application on the database the settings name: a file, or a running
    `serve-db`."""
    settings = _settings(args)
    target = settings["gracia.db"]
    kwargs = {
        "cache_dir": settings.get("gracia.cache_dir") or None
    }  # opened only when used
    if config.is_remote(target):
        kwargs["token"] = config.read_token(settings)
    elif not Path(target).exists() and args.command != "repo":
        raise ValueError(f"no such database: {target}")
    return Application(target, **kwargs)


class _OwnedStore(Store):
    """A store that closes the application it came from (a remote store needs its
    own)."""

    application: Application

    def close(self) -> None:
        super().close()
        self.application.close()


def _store(args, create: bool = False) -> Store:
    """Open the database. Only `import` creates one; other commands refuse a missing
    file (DuckDB would otherwise silently create an empty database). A `quack:` database
    is served by a running `gracia serve-db`: the repository must exist there
    already."""
    settings = _settings(args)
    target = settings["gracia.db"]
    if config.is_remote(target):
        application = _application(args)
        try:
            store = application.open_repository(args.repo)
        except NoSuchRepository:
            application.close()
            raise
        store.__class__ = _OwnedStore
        store.application = application
        return store
    if not create and not Path(target).exists():
        raise ValueError(f"no such database: {target}")
    return Store(
        target,
        triples=args.triples,
        repo=args.repo,
    )


@contextlib.contextmanager
def _ns_prefix(args, store: Store):
    """The `ns_prefix` extension of the repository the store is on (the prefixes are its
    settings, in the repository record): without a record, the defaults, and nothing can
    be written."""
    from gracia.extensions.ns_prefix import NsPrefixExtension

    application = getattr(store, "application", None)
    own = application is None
    if own:
        application = _application(args)
    try:
        yield NsPrefixExtension(Repository(args.repo, application))
    finally:
        if own:
            application.close()


def _prefix_dict(ns_prefix) -> dict[str, str]:
    return {
        item["prefix"]: item["namespace"] for item in ns_prefix.settings["prefixes"]
    }


def _save_prefixes(ns_prefix, prefixes: dict[str, str]) -> None:
    ns_prefix.save_settings(
        {"prefixes": [{"prefix": p, "namespace": n} for p, n in prefixes.items()]}
    )


@contextlib.contextmanager
def _hooks(args):
    """The repository (-r) for an import, or None when it has no repository record to
    know its types from (a plain triple store). The store that is given
    `configure_store(store)` runs the SQL of the predicates of the types on what it
    stores."""
    try:
        with _repository(args) as node:
            try:
                node.resource()
            except LookupError:
                yield None
                return
            yield node
    except NoSuchRepository:
        yield None


def cmd_import(args) -> int:
    with _store(args, create=True) as store, _hooks(args) as node:
        if node is not None:
            node.configure(store)
        for name in args.files:
            fmt = args.format or guess_format(name)
            declared: dict[str, str] = {}
            if fmt == JSON_TRIPLES:
                store.stage_json(name)
            else:
                rows = streaming_parse(name, fmt, prefixes=declared)
                store.stage_rows(rows)
            try:
                warnings = store.check()
                for warning in warnings:
                    print(f"WARNING: {name}: {warning}", file=sys.stderr)
                if warnings and args.strict:
                    print("STRICT: not loading", file=sys.stderr)
                    return 2
                counts = store.load()
                with _ns_prefix(args, store) as ns_prefix:
                    known = _prefix_dict(ns_prefix)
                    new = {p: n for p, n in declared.items() if p and p not in known}
                    if new:  # a prefix that is there is never re-bound
                        with contextlib.suppress(
                            LookupError
                        ):  # a plain triple store: no record to keep them in
                            _save_prefixes(ns_prefix, {**known, **new})
                for prefix, namespace in sorted(declared.items()):
                    if prefix in known and known[prefix] != namespace:
                        print(
                            f"WARNING: {name}: prefix {prefix!r} is {known[prefix]}, "
                            f"the file has {namespace}",
                            file=sys.stderr,
                        )
            finally:
                store.drop_staging()
            if node is not None:
                node.sync_roles(store)  # an import may have made or changed types
            past = {"insert": "inserted", "delete": "deleted"}
            summary = ", ".join(
                f"{n} rows {past.get(a, a)}" for a, n in sorted(counts.items())
            )
            print(f"{name}: {summary or 'nothing to do'}")
    return 0


def _prefixes(
    args, store: Store, values: list[str] | None, used_only: bool = False
) -> dict[str, str]:
    """The prefixes of the repository, overridden by --prefix NAME=URI options."""
    with _ns_prefix(args, store) as extension:
        prefixes = _prefix_dict(extension)
    if used_only:
        prefixes = store.used_prefixes(prefixes)
    for value in values or []:
        name, sep, uri = value.partition("=")
        if not sep:
            raise ValueError(f"--prefix expects NAME=URI, got {value!r}")
        prefixes[name] = uri
    return prefixes


def _write(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def cmd_export(args) -> int:
    with _store(args) as store:
        if args.format == JSON_TRIPLES and not args.xslt:
            _write(store.export_json(args.uri), args.output)
            return 0
        prefixes = _prefixes(args, store, args.prefix, used_only=True)
        if args.format == "ntriples" and not args.xslt and args.output:
            store.export_ntriples(
                Path(args.output), args.uri
            )  # DuckDB writes the file itself
            return 0
        # the records' N-Triples, written by DuckDB, are read back as rows by the parser
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "export.nt"
            store.export_ntriples(source, args.uri)
            rows = streaming_parse(source, "ntriples")
            if args.xslt or args.format in xslt.FORMATS:
                # in memory: the resources as flat RDF/XML, nested by a stylesheet, then
                # yours
                flat = streaming_serialize(rows, "rdfxml", prefixes=prefixes)
                if args.xslt:
                    _write(xslt.transform(flat, xslt.NESTED, args.xslt), args.output)
                else:
                    _write(xslt.render(args.format, flat), args.output)
            elif args.output:  # streaming: nothing is held in memory
                streaming_serialize(
                    rows, args.format, output=args.output, prefixes=prefixes
                )
            else:
                sys.stdout.write(
                    streaming_serialize(rows, args.format, prefixes=prefixes)
                )
    return 0


def cmd_transform(args) -> int:
    """Run a stylesheet over an XML document (a file, or stdin): no database needed."""
    params = {}
    for value in args.param or []:
        name, sep, text = value.partition("=")
        if not sep:
            raise ValueError(f"--param expects NAME=VALUE, got {value!r}")
        params[name] = text
    if args.input:
        xml_text = Path(args.input).read_text(encoding="utf-8")
    elif (
        sys.stdin.isatty()
    ):  # nothing piped in: reading would just wait for typed input
        raise ValueError(
            "no input: give an XML file, or pipe XML in, "
            "e.g. gracia export -f rdfxml-collection | gracia transform STYLESHEET"
        )
    else:
        xml_text = sys.stdin.read()
    _write(xslt.transform(xml_text, args.stylesheet, params=params), args.output)
    return 0


def cmd_prefixes(args) -> int:
    """List, set or remove prefixes: the settings of the `ns_prefix` extension of the
    repository."""
    from gracia.extensions import ExtensionError

    with _store(args) as store, _ns_prefix(args, store) as extension:
        current = _prefix_dict(extension)
        if args.remove and args.remove not in current:
            print(f"no such prefix: {args.remove}", file=sys.stderr)
            return 1
        if args.set or args.remove:
            current.pop(args.remove, None)
            for value in args.set:
                name, sep, uri = value.partition("=")
                if not sep:
                    raise ValueError(f"expected NAME=URI, got {value!r}")
                current[name] = uri
            try:
                _save_prefixes(extension, current)
            except LookupError:
                print(
                    "this repository has no record to keep prefixes in", file=sys.stderr
                )
                return 1
            except ExtensionError as err:
                print(f"invalid prefix: {err}", file=sys.stderr)
                return 1
        else:
            for name, uri in current.items():
                print(f"{name}\t{uri}")
    return 0


def cmd_delete(args) -> int:
    with _store(args) as store:
        deleted = store.delete(args.uris)
    for uri, rows in deleted.items():
        print(f"deleted {rows} rows of {uri}")
    return 0


def cmd_sort(args) -> int:
    with _store(args) as store:
        rows = store.sort()
    print(f"sorted {rows} rows")
    return 0


def cmd_etags_rebuild(args) -> int:
    """Make the etags of every record again (the repository -r): what a write keeps up
    to date as it goes, for a repository that has none yet or whose data was changed
    behind the store's back."""
    with _store(args) as store, _hooks(args) as node:
        if node is not None:
            node.configure(
                store
            ).sync()  # the numbers of its types: what the ids are made of
        wrong = store.rebuild_etags()
        made = store.count_etags()
    print(f"made {made} etags, {len(wrong)} of them different from what they were")
    return 0


def cmd_repo_list(args) -> int:
    target = _settings(args)["gracia.db"]
    if not config.is_remote(target) and not Path(target).exists():
        raise ValueError(f"no such database: {target}")
    with _application(args) as application:
        for repo in application.list_repositories():
            print(repo)
    return 0


def _admin_credentials(args, repo: str) -> tuple[str, str]:
    """The first user of a new repository: from the flags, else asked for on the
    terminal (the password is not echoed, and asked twice)."""
    asking = sys.stdin.isatty()
    userid = args.admin
    if userid is None:
        if not asking:
            raise ValueError(
                f"no terminal to ask for the admin of {repo}: give --admin USERID and "
                "--admin-password-file FILE"
            )
        userid = input(f"admin user id for {repo} [admin]: ").strip() or "admin"
    validate_userid(userid)
    if args.admin_password_file:
        password = config.read_secret(
            args.admin_password_file, minimum=passwords.MINIMUM_LENGTH
        )
    elif asking:
        password = getpass.getpass(f"password of {userid}: ")
        if getpass.getpass("again: ") != password:
            raise ValueError("the passwords differ")
    else:
        raise ValueError(
            "no terminal to ask for the password: give --admin-password-file FILE"
        )
    return userid, validate_password(password)


def cmd_repo_create(args) -> int:
    # the application creates the database file when it is missing
    with (
        _application(args) as application,
        _registry(args, application) as (_app, registry, settings),
    ):
        for repo in args.ids:
            if application.has_repository(repo):
                print(f"repository {repo} already exists")
                continue
            application.create_repository(
                repo,
                admin=_admin_credentials(args, repo),
                content_types=types.enabled(registry, settings),
            )
            print(f"created repository {repo}")
    return 0


def cmd_assets_list(args) -> int:
    """The files the types of the repositories use (all of them, or those named), each
    once, with the checksum that is part of the ETag of the records."""
    from gracia.main import make_config

    with _application(args) as application:
        settings = {k: v for k, v in _settings(args).items() if k.startswith("gracia.")}
        config = make_config({**settings, "gracia.application": application})
        config.commit()  # the registry holds the overrides of the deployment
        found = {}
        with assets.using(config.registry):
            for repo in args.ids or application.list_repositories():
                if not application.has_repository(repo):
                    raise NoSuchRepository(repo)
                node = application[repo]
                try:
                    wanted = [
                        (k.assets, k.schema, k.name)
                        for k in node.res_types.values()
                        if k.assets
                    ]
                finally:
                    node.close()
                for prefix, schema, name in wanted:
                    found.update(
                        {i.spec: i for i in assets.files_of(prefix, schema, name)}
                    )
    for spec, item in sorted(found.items()):
        print(f"{item.checksum}  {item.size:>6}  {item.media_type:<22}  {spec}")
    return 0


@contextlib.contextmanager
def _jobs(args):
    """The application, the jobs tables, the jobs directory and the settings, for a
    `jobs` command (and `run`): found the way every command finds the database."""
    from gracia.jobs import jobs_dir
    from gracia.jobs.db import JobsDb

    settings = _settings(args)
    directory = jobs_dir(settings)
    with _application(args) as application, JobsDb(application) as db:
        yield application, db, directory, settings


def _when(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else "-"


def cmd_run(args) -> int:
    """What a job's process runs (started by the server or by `gracia jobs start`)."""
    from gracia.jobs import runner
    from gracia.main import make_config

    stop = runner.StopFlag()
    stop.install()
    with _jobs(args) as (application, _db, directory, settings):
        config_ = make_config(
            {
                **{k: v for k, v in settings.items() if k.startswith("gracia.")},
                "gracia.application": application,
            }
        )
        config_.commit()  # the registry holds the overrides of the deployment
        with assets.using(config_.registry):
            return runner.run_job(application, args.uuid, directory, settings, stop)


def _current_stage(job: dict) -> dict | None:
    """The stage a job is in: the first that is not done (the last when all are)."""
    stages = (job.get("progress") or {}).get("stages") or []
    return next(
        (s for s in stages if s["state"] != "done"), stages[-1] if stages else None
    )


def _rejected(job: dict) -> int:
    stages = (job.get("progress") or {}).get("stages") or []
    return sum(sum(s["rejected"].values()) for s in stages)


def cmd_jobs_list(args) -> int:
    from gracia.jobs import procs, retention

    with _jobs(args) as (_app, db, directory, settings):
        procs.reconcile(db, directory)
        keep = retention(settings)
        rows = []
        for job in db.list_jobs(args.state, args.repo):
            expires = None
            if job["finished_at"]:
                expires = job["finished_at"] + datetime.timedelta(seconds=keep)
            rows.append((job, expires))
    if args.json:
        print(json.dumps([{**j, "expires_at": e} for j, e in rows], default=str))
        return 0
    if not rows:
        return 0
    print(
        f"{'ID':36}  {'STATE':8}  {'REPO':12}  {'STAGE':18}  "
        f"{'BATCHES':>9}  {'REJ':>6}  FINISHED / EXPIRES"
    )
    for job, expires in rows:
        stage = _current_stage(job)
        batches = "-"
        if stage is not None and stage["batches"]["total"] is not None:
            batches = f"{stage['batches']['done']}/{stage['batches']['total']}"
        end = f"{_when(job['finished_at'])} / {_when(expires)}" if expires else "-"
        print(
            f"{job['id']}  {job['state']:8}  {job['repo']:12}  "
            f"{(stage['name'] if stage else '-')[:18]:18}  {batches:>9}  "
            f"{_rejected(job):>6}  {end}"
        )
    return 0


def cmd_jobs_show(args) -> int:
    from gracia.jobs import procs

    with _jobs(args) as (_app, db, directory, _cfg):
        procs.reconcile(db, directory)
        job = db.get(args.uuid)
        if job is None:
            raise ValueError(f"no such job: {args.uuid}")
    for key in (
        "id",
        "repo",
        "job",
        "subject",
        "state",
        "message",
        "error_msg",
        "download_file",
        "download_content_type",
        "host",
        "pid",
        "attempt",
        "created_at",
        "started_at",
        "heartbeat_at",
        "finished_at",
    ):  # fmt: skip
        if job[key] is not None:
            print(f"{key:22} {job[key]}")
    print(f"{'directory':22} {directory / job['id']}")
    stages = (job.get("progress") or {}).get("stages") or [
        {
            "name": e["stage"],
            "state": "pending",
            "batches": {"done": 0, "total": None},
            "processed": 0,
            "passed": 0,
            "rejected": {},
            "samples": [],
        }
        for e in job["stages"]
    ]
    for index, stage in enumerate(stages):
        batches = stage["batches"]
        total = "?" if batches["total"] is None else batches["total"]
        rejected = (
            ", ".join(f"{n} {why}" for why, n in stage["rejected"].items()) or "none"
        )
        print(
            f"  {index + 1}. {stage['name']:14} {stage['state']:8} batches "
            f"{batches['done']}/{total}"
            f"  processed {stage['processed']}  passed {stage['passed']}  "
            f"rejected: {rejected}"
        )
        for sample in stage["samples"]:
            print(
                f"       {sample['id']}: "
                f"{sample.get('message') or sample.get('reason')}"
            )
    return 0


def cmd_jobs_log(args) -> int:
    """The log of a job: `luigi.log` (logging, from the process and its workers) or
    `job.log` (its raw output); `--follow` goes on printing what is added until it is
    interrupted."""
    import time

    from gracia.jobs import procs

    with _jobs(args) as (_app, db, directory, _cfg):
        procs.reconcile(db, directory)
        if db.get(args.uuid) is None:
            raise ValueError(f"no such job: {args.uuid}")
    path = directory / args.uuid / ("job.log" if args.raw else "luigi.log")
    offset = 0
    if path.is_file() and not args.all:
        offset = max(0, path.stat().st_size - 16 * 1024)
    try:
        while True:
            if path.is_file():
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
                    offset += len(data)
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            if not args.follow:
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _make_job(args, spec: dict) -> int:
    from gracia.jobs import procs
    from gracia.jobs.spec import JobSpecError, validate_spec

    with _jobs(args) as (application, db, directory, _cfg):
        try:
            spec = validate_spec(spec)
        except JobSpecError as err:
            print("gracia: invalid job", file=sys.stderr)
            for where, message in err.errors.items():
                print(f"  {where}: {message}", file=sys.stderr)
            return 1
        if not application.has_repository(spec["repo"]):
            raise NoSuchRepository(spec["repo"])
        job_id = db.create(spec)
        (directory / job_id).mkdir(parents=True, exist_ok=True)
        print(job_id)
        if not args.no_start:
            pid = procs.start_job(db, job_id, directory, _config_path(args), args.db)
            print(
                f"started (pid {pid}); `gracia jobs show {job_id}` for the progress",
                file=sys.stderr,
            )
    return 0


def cmd_jobs_create(args) -> int:
    text = (
        sys.stdin.read()
        if args.spec == "-"
        else Path(args.spec).read_text(encoding="utf-8")
    )
    try:
        spec = json.loads(text)
    except ValueError as err:
        raise ValueError(f"the job is not JSON: {err}") from None
    return _make_job(args, spec)


def cmd_jobs_validate(args) -> int:
    """Validate the records of a type against a schema file (a candidate revision,
    say)."""
    from gracia.jobs.validate import ValidateJob

    params = {"res_type": args.res_type, "size": args.size, "schema": args.schema}
    return _make_job(args, ValidateJob().spec(args.repo, params))


def cmd_jobs_start(args) -> int:
    from gracia.jobs import procs

    with _jobs(args) as (_app, db, directory, _cfg):
        procs.reconcile(db, directory)
        try:
            pid = procs.start_job(db, args.uuid, directory, _config_path(args), args.db)
        except KeyError:
            raise ValueError(f"no such job: {args.uuid}") from None
        except procs.JobStateError as err:
            raise ValueError(f"cannot start: {err}") from None
    print(f"started (pid {pid})")
    return 0


def cmd_jobs_stop(args) -> int:
    from gracia.jobs import procs

    with _jobs(args) as (_app, db, _directory, settings):
        grace = args.grace
        if grace is None:
            grace = float(settings.get("gracia.jobs_stop_grace") or procs.DEFAULT_GRACE)
        try:
            outcome = procs.stop_job(
                db, args.uuid, grace, args.now, who=args.by or "the command line"
            )
        except KeyError:
            raise ValueError(f"no such job: {args.uuid}") from None
    print(
        {
            "stopped": "stopped",
            "already": "already finished: nothing to stop",
            "stopping": "someone else is already stopping it",
        }[outcome]
    )
    return 0


def cmd_jobs_clean(args) -> int:
    from gracia.jobs import cleanup, procs, retention

    with _jobs(args) as (_app, db, directory, settings):
        procs.reconcile(db, directory)
        report = cleanup.clean(
            db, directory, retention(settings, args.older_than), args.dry_run
        )
    verb = "would remove" if report.dry_run else "removed"
    print(
        f"{verb} {len(report.jobs)} jobs and {len(report.orphans)} stray directories "
        f"({report.bytes / 1024:.0f} KB)"
    )
    return 0


@contextlib.contextmanager
def _registry(args, application=None):
    """The application, the settings and a copy of the registry of the application (the
    types it knows, with the record classes and the rest), made the way the web
    application makes its own: the commands that deal with types ask it, as the web
    application does."""
    from gracia.main import make_config

    settings = {k: v for k, v in _settings(args).items() if k.startswith("gracia.")}
    if application is not None:
        config_ = make_config({**settings, "gracia.application": application})
        config_.commit()
        yield application, config_.registry, settings
        return
    with Application(":memory:") as stand_in:  # the types need no database to be listed
        config_ = make_config({**settings, "gracia.application": stand_in})
        config_.commit()
        yield stand_in, config_.registry, settings


def cmd_types_available(args) -> int:
    """The content types there are (the ones that can be added to a repository)."""
    with _registry(args) as (_app, registry, _cfg):
        for res_type in types.content_types(registry).values():
            print(
                f"{res_type.slug:16} {res_type.class_uri}  {res_type.schema}  "
                f"({res_type.package})"
            )
    return 0


def cmd_types_list(args) -> int:
    """The types of the repository (-r): its slug, the record of the type, its name and
    class."""
    with _application(args) as application:
        if not application.has_repository(args.repo):
            raise NoSuchRepository(args.repo)
        node = application[args.repo]
        try:
            for res_type in node.res_types.values():
                print(
                    f"{res_type.slug:16} {res_type.record:20} {res_type.name:12} "
                    f"{res_type.class_uri}  {res_type.schema or '-'}"
                )
        finally:
            node.close()
    return 0


@contextlib.contextmanager
def _repository(args):
    """The repository node (-r), with the registry of the application made the way the
    web application makes its own (that is where the extensions are)."""
    from gracia.main import make_config

    settings = {k: v for k, v in _settings(args).items() if k.startswith("gracia.")}
    with _application(args) as application:
        if not application.has_repository(args.repo):
            raise NoSuchRepository(args.repo)
        config_ = make_config({**settings, "gracia.application": application})
        config_.commit()
        config_.begin()
        node = application[args.repo]
        try:
            yield node
        finally:
            node.close()
            config_.end()


def _extension(node, name: str):
    try:
        return node.extension(name)
    except KeyError:
        raise ValueError(f"there is no extension {name!r}") from None


def cmd_extensions_list(args) -> int:
    """The extensions there are and whether each is on in the repository (-r)."""
    with _repository(args) as node:
        for extension in node.extensions():
            state = "enabled" if extension.enabled else "disabled"
            fixed = (
                "  (cannot be disabled)"
                if extension.enabled and not extension.can_disable
                else ""
            )
            print(f"{extension.name:16} {state:9} {extension.title}{fixed}")
    return 0


def cmd_extensions_enable(args) -> int:
    with _repository(args) as node:
        _extension(node, args.name).enable()
    print(f"{args.name}: enabled")
    return 0


def cmd_extensions_disable(args) -> int:
    with _repository(args) as node:
        _extension(node, args.name).disable()
    print(f"{args.name}: disabled (its settings are kept)")
    return 0


def cmd_extensions_export(args) -> int:
    """The settings of an extension as JSON, the text the Extensions tab shows."""
    with _repository(args) as node:
        text = _extension(node, args.name).export()
    _write(text, args.output)
    return 0


def cmd_extensions_import(args) -> int:
    """Set the settings of an extension from JSON (a file, or stdin), checked like the
    form does."""
    text = (
        sys.stdin.read()
        if args.file in (None, "-")
        else Path(args.file).read_text("utf-8")
    )
    with _repository(args) as node:
        _extension(node, args.name).import_json(text)
    print(f"{args.name}: settings updated")
    return 0


def cmd_types_add(args) -> int:
    """Add content types (by slug or package) to the repository (-r); the ones it has
    are kept."""
    with _application(args) as application:
        if not application.has_repository(args.repo):
            raise NoSuchRepository(args.repo)
        with _registry(args, application) as (_app, registry, _cfg):
            found = [
                types.find_content_type(registry, name) for name in args.content_types
            ]
        with application.open_repository(args.repo) as store:
            for res_type in found:
                record = types.add_content_type(store, args.repo, res_type)
                print(
                    f"{res_type.slug}: added as {record}"
                    if record
                    else f"{res_type.slug}: {args.repo} has it already"
                )
            node = Repository(args.repo, application)
            try:
                node.sync_roles(
                    store
                )  # the shapes of the new types say what their records have
            finally:
                node.close()
    return 0


def cmd_serve_db(args) -> int:
    settings = config.read(config.config_file(args.config))
    db = args.db or settings.get("gracia.db.file") or os.environ.get("GRACIA_DB")
    if not db and not config.is_remote(settings.get("gracia.db", "quack:")):
        db = settings.get("gracia.db") or config.DEFAULT_DB
    db = db or config.DEFAULT_DB
    host, port = args.host, args.port
    target = settings.get("gracia.db", "")
    if config.is_remote(target):  # the address the clients use is the one to serve
        ini_host, ini_port = config.parse_target(target)
        host = host or ini_host
        port = port or ini_port
    host, port = host or "localhost", port or DEFAULT_PORT
    token_file = args.token_file or settings.get("gracia.db.token_file")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def ready(uri, token_file):
        print(f"serving {db} on {uri} (token in {token_file})", flush=True)

    serve_db(db, host, port, token_file, stop=stop, ready=ready)
    print("stopped", flush=True)
    return 0


def cmd_serve(args) -> int:
    from waitress import serve as waitress_serve

    from gracia.main import make_app

    settings = _settings(args)
    found = _config_path(args)
    if found:  # the jobs the server starts are given the same file
        settings = {**settings, "gracia.config_file": found}
    app = make_app({k: v for k, v in settings.items() if k.startswith("gracia.")})
    try:
        print(
            f"serving {settings['gracia.db']} on http://{args.host}:{args.port}/ "
            f"({args.threads} threads)",
            flush=True,
        )
        waitress_serve(app, host=args.host, port=args.port, threads=args.threads)
    finally:
        if app.registry.get("application_owned"):
            app.registry["application"].close()
    return 0


def cmd_formats(_args) -> int:
    print("input (guessed from the extension, or --format):")
    for ext, fmt in sorted(INPUT_BY_EXTENSION.items()):
        print(f"  {ext:8} {fmt}")
    print("output (--format):")
    for fmt in (*OUTPUT_FORMATS, *xslt.FORMATS):
        print(f"  {fmt}")
    print("bundled stylesheets (for `gracia transform`):")
    for name in xslt.bundled_names():
        print(f"  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gracia", description=__doc__)
    parser.add_argument(
        "-c",
        "--config",
        help="config file (default: $GRACIA_CONFIG, else ./config.ini if there is one)",
    )
    parser.add_argument(
        "-d",
        "--db",
        help="DuckDB file or quack:host:port "
        f"(default $GRACIA_DB, else {config.DEFAULT_DB})",
    )
    parser.add_argument(
        "-r",
        "--repo",
        default=DEFAULT_REPOSITORY,
        help="repository, a schema of the database (default %(default)s)",
    )
    parser.add_argument(
        "--triples", default="triples", help="triples table (default %(default)s)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="load RDF files into the database")
    p.add_argument("files", nargs="+")
    p.add_argument("-f", "--format", help="input format (default: from the extension)")
    p.add_argument("--strict", action="store_true", help="refuse files with warnings")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="write the database (or one resource) as RDF")
    p.add_argument(
        "-f", "--format", default="turtle", help="output format (default %(default)s)"
    )
    p.add_argument(
        "-u",
        "--uri",
        action="append",
        help="only this resource (subject uri); repeatable",
    )
    p.add_argument(
        "-x",
        "--xslt",
        help="run this stylesheet over the nested RDF/XML instead of writing a format",
    )
    p.add_argument(
        "-p",
        "--prefix",
        action="append",
        metavar="NAME=URI",
        help="extra or overriding prefix for the output (repeatable)",
    )
    p.add_argument("-o", "--output", help="output file (default stdout)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "transform",
        help="run an XSLT stylesheet over an XML file (or stdin); bundled ones by name",
    )
    p.add_argument("stylesheet", help="a stylesheet file, or the name of a bundled one")
    p.add_argument("input", nargs="?", help="XML file (default stdin)")
    p.add_argument(
        "--param", action="append", metavar="NAME=VALUE", help="string parameter"
    )
    p.add_argument("-o", "--output", help="output file (default stdout)")
    p.set_defaults(func=cmd_transform)

    p = sub.add_parser(
        "delete", help="delete resources and everything embedded in them"
    )
    p.add_argument("uris", nargs="+")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser(
        "sort", help="rewrite the triples sorted, so a type is a few row groups"
    )
    p.set_defaults(func=cmd_sort)

    p = sub.add_parser("etags", help="the etags of the pages of the records")
    esub_ = p.add_subparsers(dest="etags_command", required=True)
    q = esub_.add_parser("rebuild", help="make the etags of every record again (-r)")
    q.set_defaults(func=cmd_etags_rebuild)

    p = sub.add_parser(
        "prefixes", help="list, set (NAME=URI) or remove namespace prefixes"
    )
    p.add_argument("set", nargs="*", metavar="NAME=URI")
    p.add_argument("--remove", metavar="NAME")
    p.set_defaults(func=cmd_prefixes)

    p = sub.add_parser("repo", help="list and create repositories (a schema each)")
    rsub = p.add_subparsers(dest="repo_command", required=True)
    q = rsub.add_parser("list", help="list the repositories")
    q.set_defaults(func=cmd_repo_list)
    q = rsub.add_parser("create", help="create repositories (idempotent)")
    q.add_argument("ids", nargs="+", metavar="ID")
    q.add_argument(
        "--admin", metavar="USERID", help="user id of the first user (else asked for)"
    )
    q.add_argument(
        "--admin-password-file",
        metavar="FILE",
        help="file with the password of the first user, mode 0600 (else asked for)",
    )
    q.set_defaults(func=cmd_repo_create)

    p = sub.add_parser(
        "serve-db",
        help="run the database as its own process, served over Quack on localhost",
    )
    p.add_argument("--host", help="localhost or 127.0.0.1 (default localhost)")
    p.add_argument("--port", type=int, help=f"(default {DEFAULT_PORT})")
    p.add_argument(
        "--token-file",
        help="file holding the token, created with mode 0600 (default <db>.token)",
    )
    p.set_defaults(func=cmd_serve_db)

    p = sub.add_parser(
        "serve", help="run the web application (one process, a thread per request)"
    )
    p.add_argument("--host", default="127.0.0.1", help="(default %(default)s)")
    p.add_argument("--port", type=int, default=8000, help="(default %(default)s)")
    p.add_argument(
        "--threads", type=int, default=4, help="request threads (default %(default)s)"
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("assets", help="the files of the types (shapes, stylesheets)")
    asub = p.add_subparsers(dest="assets_command", required=True)
    q = asub.add_parser("list", help="the files the types use, with their checksums")
    q.add_argument("ids", nargs="*", metavar="ID", help="repositories (default: all)")
    q.set_defaults(func=cmd_assets_list)

    p = sub.add_parser(
        "jobs", help="long jobs: list, show, log, create, start, stop, clean"
    )
    jsub = p.add_subparsers(dest="jobs_command", required=True)
    q = jsub.add_parser("list", help="the jobs, newest first")
    q.add_argument("--state", help="only jobs in this state")
    q.add_argument("--repo", help="only jobs of this repository")
    q.add_argument("--json", action="store_true", help="JSON, for scripts")
    q.set_defaults(func=cmd_jobs_list)
    q = jsub.add_parser("show", help="one job and its stages")
    q.add_argument("uuid")
    q.set_defaults(func=cmd_jobs_show)
    q = jsub.add_parser("create", help="make a job from a JSON file (or - for stdin)")
    q.add_argument("spec", metavar="FILE")
    q.add_argument(
        "--no-start", action="store_true", help="only make it, do not start it"
    )
    q.set_defaults(func=cmd_jobs_create)
    q = jsub.add_parser(
        "validate", help="validate the records of a type against a schema file"
    )
    q.add_argument("--repo", required=True)
    q.add_argument(
        "--type", dest="res_type", required=True, help="slug or name of the type"
    )
    q.add_argument(
        "--schema",
        help="<Type>-<profile>-<revision>.ttl (default: the one the type is on)",
    )
    q.add_argument(
        "--size",
        "--batch-size",
        dest="size",
        type=int,
        default=2000,
        help="record numbers per batch (default %(default)s)",
    )
    q.add_argument(
        "--no-start", action="store_true", help="only make it, do not start it"
    )
    q.set_defaults(func=cmd_jobs_validate)
    q = jsub.add_parser(
        "log", help="the log of a job (luigi.log; --raw for its output)"
    )
    q.add_argument("uuid")
    q.add_argument(
        "--raw", action="store_true", help="job.log: what the process printed"
    )
    q.add_argument(
        "--all", action="store_true", help="from the start (default: the last 16 KB)"
    )
    q.add_argument(
        "--follow", "-f", action="store_true", help="go on printing what is added"
    )
    q.set_defaults(func=cmd_jobs_log)
    q = jsub.add_parser(
        "start", help="start a queued job, or restart a stopped or failed one"
    )
    q.add_argument("uuid")
    q.set_defaults(func=cmd_jobs_start)
    q = jsub.add_parser(
        "stop", help="stop a job: SIGTERM, then SIGKILL after the grace period"
    )
    q.add_argument("uuid")
    q.add_argument(
        "--grace",
        type=float,
        help="seconds before SIGKILL (default: gracia.jobs_stop_grace, 15)",
    )
    q.add_argument("--now", action="store_true", help="SIGKILL at once")
    q.add_argument(
        "--by", help="who asked, for the message of the job (default: the command line)"
    )
    q.set_defaults(func=cmd_jobs_stop)
    q = jsub.add_parser("clean", help="remove finished jobs and their directories")
    q.add_argument(
        "--older-than",
        metavar="AGE",
        help="30m, 8h, 2d (default: gracia.jobs_retention, 8h)",
    )
    q.add_argument(
        "--dry-run", action="store_true", help="only say what would be removed"
    )
    q.set_defaults(func=cmd_jobs_clean)

    p = sub.add_parser(
        "types", help="the types of a repository and the content types there are"
    )
    ksub = p.add_subparsers(dest="types_command", required=True)
    q = ksub.add_parser("available", help="the content types that can be added")
    q.set_defaults(func=cmd_types_available)
    q = ksub.add_parser("list", help="the types of the repository (-r)")
    q.set_defaults(func=cmd_types_list)
    q = ksub.add_parser(
        "add", help="add content types, by slug or package, to the repository (-r)"
    )
    q.add_argument("content_types", nargs="+", metavar="CONTENT_TYPE")
    q.set_defaults(func=cmd_types_add)

    p = sub.add_parser(
        "extensions", help="the extensions of a repository: on or off, their settings"
    )
    esub = p.add_subparsers(dest="extensions_command", required=True)
    q = esub.add_parser(
        "list", help="the extensions and whether they are on in the repository (-r)"
    )
    q.set_defaults(func=cmd_extensions_list)
    q = esub.add_parser("enable", help="turn an extension on in the repository (-r)")
    q.add_argument("name")
    q.set_defaults(func=cmd_extensions_enable)
    q = esub.add_parser("disable", help="turn an extension off in the repository (-r)")
    q.add_argument("name")
    q.set_defaults(func=cmd_extensions_disable)
    q = esub.add_parser("export", help="the settings of an extension as JSON")
    q.add_argument("name")
    q.add_argument("-o", "--output", help="output file (default stdout)")
    q.set_defaults(func=cmd_extensions_export)
    q = esub.add_parser("import", help="set the settings of an extension from JSON")
    q.add_argument("name")
    q.add_argument("file", nargs="?", help="a JSON file (default stdin)")
    q.set_defaults(func=cmd_extensions_import)

    p = sub.add_parser(
        "run", help="run one job in this process (what `jobs start` spawns)"
    )
    p.add_argument("uuid")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("formats", help="list the supported formats")
    p.set_defaults(func=cmd_formats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RdfError, RuntimeError, ValueError, OSError, duckdb.Error) as err:
        print(f"gracia: {err}", file=sys.stderr)
        if "Conflicting lock" in str(err):
            print(
                "gracia: another process owns this database file (`gracia serve-db`). "
                "Commands use it automatically through the <db>.owner and <db>.token "
                "files next to the database, or name it in a config file (-c: "
                "gracia.db = quack:host:port and gracia.db.token_file).",
                file=sys.stderr,
            )
            if _probe_error:
                print(f"gracia: {_probe_error}", file=sys.stderr)
        return 1
