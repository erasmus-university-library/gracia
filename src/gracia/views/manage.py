import inspect
from html import escape

import colander
import deform
import pyoxigraph as ox
from pyramid.httpexceptions import HTTPBadRequest, HTTPFound, HTTPNotFound
from pyramid.response import Response
from pyramid.view import view_config

from gracia import forms, predicates, search, static, uri
from gracia.extensions import ExtensionError, settings_form
from gracia.forms import Form
from gracia.jobs.job import all_jobs
from gracia.resources import Record, Repository, Type
from gracia.utils import assets
from gracia.views import View, flash, fragment, same_origin


def _size(number) -> str:
    number = float(number or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1000 or unit == "TB":
            return (
                f"{number:.0f} {unit}"
                if unit == "B" or number >= 100
                else f"{number:.1f} {unit}"
            )
        number /= 1000
    return ""


def _when(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


class Manage(View):
    template = "gracia:assets/templates/manage.pt"
    scope = ""  # "repository" or "type": the form of a pane is `form-<scope>-<name>`
    panes: tuple[tuple[str, str], ...] = ()  # (name, title) in the order of the strip

    def heading(self) -> str:
        raise NotImplementedError

    def subtitle(self) -> str:
        raise NotImplementedError

    def form_id(self, name: str) -> str:
        return f"form-{self.scope}-{name}"

    def action(self, pane: str) -> str:
        return self.request.resource_url(self.context, "manage", query={"tab": pane})

    def page(self) -> Response:
        request = self.request
        names = [name for name, _ in self.panes]
        active = request.GET.get("tab")
        states: dict[str, dict] = {}
        if request.method == "POST":
            same_origin(
                request
            )  # it changes data, and the browser sends the credentials itself
            result = self._post(request.POST.get("__formid__", ""))
            if isinstance(result, Response):
                return result
            active = active if active in names else names[0]
            states[active] = result
        if active not in names:
            active = names[0]
        panes = [
            {
                "name": name,
                "title": title,
                "active": name == active,
                "html": getattr(self, f"pane_{name}")(states.get(name, {})),
            }
            for name, title in self.panes
        ]
        static.jobs_js.need()  # the job panel that a pane may include
        static.manage_js.need()
        return self.render(
            heading=self.heading(), subtitle=self.subtitle(), panes=panes
        )

    def _post(self, form_id: str):
        parts = form_id.split("-")
        for end in range(len(parts), 1, -1):  # the longest name that is a handler
            handler = getattr(self, "post_" + "_".join(parts[1:end]), None)
            if handler is not None:
                return handler("-".join(parts[end:]))
        raise HTTPBadRequest("no such form")


class RepositoryManage(Manage):
    scope = "repository"
    panes = (
        ("types", "Types"),
        ("settings", "Settings"),
        ("extensions", "Extensions"),
        ("search", "Search"),
        ("predicates", "Predicates"),
        ("stats", "Stats"),
        ("jobs", "Jobs"),
    )

    @view_config(context=Repository, name="manage", permission="manage")
    def manage(self):
        return self.page()

    def heading(self) -> str:
        return f"Manage {self.context.__name__}"

    def subtitle(self) -> str:
        return self.context.uri

    # ---- Types
    # ------------------------------------------------------------------------------

    def pane_types(self, state):
        request = self.request
        records = request.store.count_records_by_type()  # the records of each type
        types = [
            {
                "records": records.get(res_type.slug, 0),
                "name": res_type.name,
                "slug": res_type.slug,
                # what the shape of the type says of itself (the schema file it is on)
                "description": forms.shape_description(*shape)
                if (shape := res_type.shape())
                else "",
                "system": res_type.class_uri in uri.SYSTEM_CLASSES,
                "url": (
                    request.resource_url(res_type, "manage")
                    if request.has_permission("manage", res_type)
                    else None
                ),
            }
            for res_type in self.context.types.values()
        ]
        return fragment(
            request, "gracia:assets/templates/repository_tab_types.pt", types=types
        )

    # ---- Settings
    # ---------------------------------------------------------------------------

    def _settings_form(self) -> tuple[Form, colander.Schema]:
        schema = colander.Schema()
        for node in forms.schema_from_shape(*self.context.shape()).children:
            code = predicates.find(
                node.predicate
            )  # another widget, a rule: what a predicate adds
            changed = (
                code.schema_node(None, self.request, node) if code is not None else node
            )
            if changed is not None:
                changed.predicate = node.predicate
                schema.add(changed)
        form = Form(
            schema,
            formid=self.form_id("settings"),
            action=self.action("settings"),
            buttons=("save",),
        )
        return form, schema

    def pane_settings(self, state):
        form, schema = self._settings_form()
        static.need_form(form)
        if "html" in state:  # the form as it was posted, with what was wrong
            return state["html"]
        record = self.context.record()
        current = {
            n.name: next(
                (o.value for o in record.graph.objects(record.resource, n.predicate)),
                "",
            )
            for n in schema.children
        }
        return form.render(current)

    def post_repository_settings(self, _rest):
        repository = self.context
        form, schema = self._settings_form()
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        record = repository.record()
        subject, resource = record.resource, record.graph
        for node in schema.children:
            predicate = ox.NamedNode(node.predicate)
            for quad in [
                q
                for q in resource.quads_for_subject(subject)
                if q.predicate == predicate
            ]:
                resource.discard(quad)
            if data[node.name]:
                resource.add(
                    ox.Quad(
                        subject,
                        predicate,
                        ox.Literal(data[node.name]),
                        ox.DefaultGraph(),
                    )
                )
        record.save()  # the label of the repository follows its title
        flash(self.request, "Saved.")
        return HTTPFound(self.action("settings"))

    # ---- Extensions
    # -------------------------------------------------------------------------
    # A card for every extension there is: on or off, and a dialog with its settings.
    # The dialog
    # has two tabs, the Deform form of the extension and the same settings as JSON to
    # copy or
    # paste. Each of its forms names itself `form-extension-<name>`, and what a button
    # does is the
    # name of the button.

    def _extension_form(self, extension):
        return settings_form(
            extension,
            self.request,
            formid=f"form-extension-{extension.name}",
            action=self.action("extensions"),
            buttons=(deform.Button(name="update", title="Update settings"),),
        )

    def pane_extensions(self, state):
        cards = []
        for extension in self.context.extensions():
            name = extension.name
            settings = extension.settings_schema is not None and extension.enabled
            asked = state.get("open") == name
            form_html = ""
            if settings:
                form = self._extension_form(extension)
                static.need_form(form)
                form_html = (
                    state["html"]
                    if asked and "html" in state
                    else form.render(extension.settings)
                )
            cards.append(
                {
                    "name": name,
                    "form_id": f"form-extension-{name}",
                    "title": extension.title or name,
                    "description": extension.description,
                    "enabled": extension.enabled,
                    "can_disable": extension.can_disable,
                    "settings": settings,
                    "url": self.action("extensions"),
                    "form_html": form_html,
                    "json": (state.get("json") if asked and "json" in state else None)
                    or (extension.export() if settings else ""),
                    "open": asked,
                    "inner": state.get("inner", "settings") if asked else "settings",
                    "errors": state.get("errors", []) if asked else [],
                }
            )
        if any(card["settings"] for card in cards):
            static.editor_js.need()  # the settings as JSON are an editor
        return fragment(
            self.request,
            "gracia:assets/templates/repository_tab_extensions.pt",
            cards=cards,
        )

    def post_extension(self, name):
        request = self.request
        try:
            extension = self.context.extension(name)
        except KeyError:
            raise HTTPNotFound(f"there is no extension {name!r}") from None
        title = extension.title or name
        try:
            if "enable" in request.POST:
                extension.enable()
                flash(request, f"{title} is enabled.")
            elif "disable" in request.POST:
                extension.disable()
                flash(request, f"{title} is disabled. Its settings are kept.")
            elif "update" in request.POST:
                form = self._extension_form(extension)
                try:
                    data = form.validate(list(request.POST.items()))
                except deform.ValidationFailure as failure:
                    static.need_form(form)
                    return {"open": name, "inner": "settings", "html": failure.render()}
                extension.save_settings(data)
                flash(request, f"The settings of {title} are updated.")
            elif "import_json" in request.POST:
                extension.import_json(request.POST.get("json", ""))
                flash(request, f"The settings of {title} are updated from JSON.")
            else:
                raise HTTPBadRequest("nothing to do")
        except ExtensionError as err:
            return {
                "open": name,
                "inner": "json" if "import_json" in request.POST else "settings",
                "errors": err.errors,
                "json": request.POST.get("json"),
            }
        return HTTPFound(self.action("extensions"))

    # ---- Search
    # -----------------------------------------------------------------------------
    # The fields the docs can have (the schema mode of the doc stylesheets of the types)
    # against the
    # fields of
    # the indexes (the settings of the Search extension). A field the docs have and the
    # index lacks
    # is shown
    # greyed, with a button to add it to the settings; the button of the others takes it
    # out.
    # Changing the
    # settings is all it does: an index that is not what they say is stale, and the job
    # Rebuild
    # search makes it.

    def pane_search(self, state):
        request, repository = self.request, self.context
        application = request.root
        indexes = application.indexes
        notice = None
        try:
            extension = repository.extension("search")
        except KeyError:
            extension = None
        if indexes is None:
            notice = "No search index: set gracia.index_dir."
        elif extension is None or not extension.enabled:
            notice = "Search is turned off in this repository (the Search extension)."
        sections, problem = [], None
        if notice is None:
            try:
                offered = application.cache.docs.schema(
                    repository, list(repository.types)
                )
            except (
                Exception
            ) as err:  # a stylesheet that does not work is a message, not a failed page
                offered, problem = (
                    [],
                    f"The doc stylesheets could not make the schema: {err}",
                )
            for definition in search.definitions_for(repository):
                rows = search.compare(offered, definition)
                counts = indexes.field_stats(repository.__name__, definition)
                for row in rows:
                    row["docs"] = (
                        f"{counts[row['name']]:,}"
                        if counts.get(row["name"]) is not None
                        else None
                    )
                    row["measured"] = row["name"] in counts
                    row["every_type"] = bool(row["types"]) and set(row["types"]) == set(
                        repository.types
                    )
                stats = indexes.stats(repository.__name__, [definition])
                documents = stats[0]["documents"]
                sections.append(
                    {
                        "name": definition.name,
                        "state": indexes.status(repository.__name__, definition),
                        "documents": f"{documents:,}",
                        "rows": rows,
                    }
                )
        jobs_node = repository["jobs"]
        job = next(
            (j for j in all_jobs(request.registry) if j.name == "rebuild_search"), None
        )
        rebuild = (
            {
                "title": job.title,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            if job and request.has_permission(job.start_permission, jobs_node)
            else None
        )
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_search.pt",
            notice=notice,
            problem=problem,
            errors=state.get("errors", []),
            sections=sections,
            rebuild=rebuild,
            action=self.action("search"),
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    def post_repository_search(self, _rest):
        request, repository = self.request, self.context
        post = request.POST
        index = post.get("index", "")
        try:
            extension = repository.extension("search")
            if "add" in post:
                offered = request.root.cache.docs.schema(
                    repository, list(repository.types)
                )
                field = next((f for f in offered if f["name"] == post["add"]), None)
                if field is None:
                    raise ExtensionError([f"no doc has a field {post['add']!r}"])
                extension.add_field(index, field)
                flash(
                    request,
                    f"{post['add']} is in the index {index}: "
                    "the index needs a rebuild.",
                )
            elif "remove" in post:
                extension.remove_field(index, post["remove"])
                flash(
                    request,
                    f"{post['remove']} is out of the index {index}: "
                    "the index needs a rebuild.",
                )
            else:
                raise HTTPBadRequest("nothing to do")
        except KeyError:
            raise HTTPNotFound("this repository has no Search extension") from None
        except ExtensionError as err:
            return {"errors": err.errors}
        return HTTPFound(self.action("search"))

    # ---- Predicates
    # -------------------------------------------------------------------------

    def _titles(self) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}

        def walk(dataset, shape):
            for path, prop in forms.properties(dataset, shape):
                found.setdefault(
                    path,
                    (
                        forms.property_title(dataset, prop, ""),
                        forms.shape_description(dataset, prop),
                    ),
                )
                nested = forms.nested_shape(dataset, prop)
                if nested is not None:
                    walk(dataset, nested)

        for res_type in self.context.types.values():
            walk(*res_type.record_shape())
        return found

    def pane_predicates(self, state):
        used = self.store.count_by_predicate()  # the values there are of each property
        titles = self._titles()
        cards = []
        for code in predicates.all_predicates():
            title, description = titles.get(code.uri, ("", ""))
            cards.append(
                {
                    "uri": code.uri,
                    "title": title or code.uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1],  # ty: ignore[missing-union-attribute]
                    "description": description,
                    "name": f"{type(code).__module__}.{type(code).__qualname__}",
                    # the class's own docstring (getdoc would take the base class's),
                    # without markup
                    "doc": inspect.cleandoc(type(code).__doc__ or "").replace("`", ""),
                    "values": used.get(code.uri, 0),
                    "parts": [
                        part
                        for part in ("schema_node", "on_write_sql")
                        if getattr(type(code), part)
                        is not getattr(predicates.Predicate, part)
                    ],
                }
            )
        cards.sort(key=lambda card: card["uri"])  # ty: ignore[no-matching-overload]
        return fragment(
            self.request,
            "gracia:assets/templates/repository_tab_predicates.pt",
            cards=cards,
        )

    # ---- Stats
    # ------------------------------------------------------------------------------
    # What there is: the types, the caches, and what waits for the worker that follows
    # the writes.
    # The
    # buttons start the jobs of the Jobs tab, whose panel (it is on the page) follows
    # them.
    def pane_stats(self, state):
        request, repository = self.request, self.context
        store, application = request.store, request.root
        installed = {res_type.slug: res_type for res_type in repository.types.values()}
        found = store.type_stats()
        empty = {"records": 0, "published": 0, "bytes": 0, "modified": None}
        types = []
        for slug in [*installed, *(slug for slug in found if slug not in installed)]:
            stats, res_type = found.get(slug, empty), installed.get(slug)
            types.append(
                {
                    "name": res_type.name if res_type else slug,
                    "slug": slug,
                    "system": bool(
                        res_type and res_type.class_uri in uri.SYSTEM_CLASSES
                    ),
                    "records": stats["records"],
                    "published": stats["published"],
                    "size": _size(stats["bytes"]),
                    "average": _size(stats["bytes"] / stats["records"])
                    if stats["records"]
                    else "",
                    "modified": _when(stats["modified"]),
                }
            )
        total = sum(stats["records"] for stats in found.values())
        totals = {
            "records": total,
            "size": _size(sum(stats["bytes"] for stats in found.values())),
            "modified": _when(
                max(
                    (s["modified"] for s in found.values() if s["modified"]),
                    default=None,
                )
            ),
        }
        cache = application.cache
        pages = store.etag_stats()
        graphs = cache.graph_stats(repository.__name__)
        held = cache.etags_held(repository.__name__)
        cache_stats = {
            "pages": pages["pages"],
            "missing": max(0, total - pages["pages"]),  # records that have no etag yet
            "pages_size": _size(pages["bytes"]),
            "modified": _when(pages["modified"]),
            "etags_held": held,
            "graphs": graphs
            and {
                "directory": graphs["directory"],
                "repo_entries": f"{graphs['repo_entries']:,}"
                + ("+" if graphs["repo_entries"] >= 100_000 else ""),
                "entries": f"{graphs['entries']:,}",
                "bytes": _size(graphs["bytes"]),
                "file": _size(graphs["file"]),
                "limit": _size(graphs["limit"]),
            },
        }
        indexes = application.indexes
        definitions = search.definitions_for(repository) if indexes else []
        rows = indexes.stats(repository.__name__, definitions) if indexes else None
        search_stats = rows and [
            {
                "name": row["name"],
                "state": row["state"],
                "documents": f"{row['documents']:,}",
                # of the records there are: what the index lacks is what is not in it
                # yet
                "of": f"{sum(s['records'] for s in found.values()):,}",
                "size": _size(row["bytes"]),
            }
            for row in rows
        ]
        pending = store.pending_summary()
        worker = cache.worker
        actions = {
            "worker": self._worker_line(worker, repository.__name__),
            "rows": pending["rows"],
            "records": pending["records"],
            "size": _size(pending["bytes"]),
            "by_action": ", ".join(
                f"{n:,} {action}" for action, n in sorted(pending["actions"].items())
            ),
            "since": _when(pending["since"]),
            "oldest": pending["oldest"],
            "last": pending["last"],
        }
        jobs_node = repository["jobs"]
        buttons = {
            job.name: {
                "title": job.title,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            for job in all_jobs(request.registry)
            if job.name in ("update_cache", "rebuild_search", "process_actions")
            and request.has_permission(job.start_permission, jobs_node)
        }
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_stats.pt",
            types=types,
            totals=totals,
            cache=cache_stats,
            search=search_stats,
            search_note=(
                "No search index: set gracia.index_dir."
                if indexes is None
                else "Search is turned off in this repository (the Search extension)."
            ),
            actions=actions,
            buttons=buttons,
            refresh_url=self.action("stats"),
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    @staticmethod
    def _worker_line(worker, repo: str) -> dict:
        if worker is None:
            return {
                "state": "off",
                "text": "Off: the setting gracia.worker is not on.",
                "ok": False,
            }
        status = worker.status(repo)
        if not status["running"]:
            text = (
                "Stopped."
                if status["stopped"]
                else "Not running. The next request starts it again."
            )
            return {"state": "stopped", "text": text, "ok": False}
        if status["failures"]:
            retry = (
                f", the next try in {status['retry_in']:.0f} s"
                if status["retry_in"] is not None
                else ""
            )
            return {
                "state": "failing",
                "text": (
                    f"Running, but this repository failed {status['failures']} "
                    f"time(s) in a row{retry}."
                ),
                "ok": False,
            }
        text = (
            "Running; this repository is waiting for its turn."
            if status["in_line"]
            else "Running; nothing to do here."
        )
        return {"state": "running", "text": text, "ok": True}

    # ---- Jobs
    # -------------------------------------------------------------------------------
    # The jobs about the repository as a whole; one is started with its button
    # (`jobs.js`).
    REPOSITORY_JOBS = (
        "rebuild_etags",
        "update_cache",
        "rebuild_search",
        "process_actions",
    )

    def pane_jobs(self, state):
        request, repository = self.request, self.context
        jobs_node = repository["jobs"]
        items = [
            {
                "name": job.name,
                "title": job.title,
                "description": job.description,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            for job in all_jobs(request.registry)
            if job.name in self.REPOSITORY_JOBS
            and request.has_permission(job.start_permission, jobs_node)
        ]
        items.sort(key=lambda item: self.REPOSITORY_JOBS.index(item["name"]))
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_jobs.pt",
            jobs=items,
            job_filter=",".join(
                self.REPOSITORY_JOBS
            ),  # the list shows the jobs of all of them
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )


class TypeManage(Manage):
    scope = "type"
    panes = (
        ("settings", "Settings"),
        ("identification", "Identification"),
        ("jobs", "Jobs"),
    )

    @view_config(context=Type, name="manage", permission="manage")
    def manage(self):
        return self.page()

    def heading(self) -> str:
        return f"Manage {self.context.name}"

    def subtitle(self) -> str:
        return self.context.record

    def _record(self) -> Record:
        res_type = self.context
        return res_type.repository["type"][res_type.record.rsplit(":", 1)[1]]

    def _form(self, record: Record) -> Form:
        return Form(
            record.edit_schema(self.request),
            formid=self.form_id("settings"),
            action=self.action("settings"),
            buttons=("save",),
        )

    def pane_settings(self, state):
        record = self._record()
        form = self._form(record)
        static.need_form(form)
        html = state["html"] if "html" in state else form.render(record.edit_values())
        errors = "".join(
            f'<div class="alert alert-danger">{escape(e)}</div>'
            for e in state.get("errors", [])
        )
        return errors + html

    # ---- Identification: the numbers of the records of the type
    # ---------------------------------
    # The numbers are given out one after the other, past the highest record there is. A
    # person can
    # reserve a swath of them (before an import of records that come with their own
    # numbers): the
    # next number is then past the number that was set. It cannot be set lower than a
    # number that
    # is in use, only higher; lowering a reservation to what is in use gives it up.

    class IdentificationForm(colander.MappingSchema):
        last = colander.SchemaNode(
            colander.Integer(),
            title="Numbers up to this are taken",
            description=(
                "The next record gets the number after this one. Set it higher to keep "
                "the numbers up to it for records that are going to be imported with "
                "numbers of their own; it cannot be lower than a number that is in use."
            ),
            validator=colander.Range(min=0, max=999_999_999_999_999),
        )

    def _identification_form(self) -> Form:
        return Form(
            self.IdentificationForm(),
            formid=self.form_id("identification"),
            action=self.action("identification"),
            buttons=("save",),
        )

    def pane_identification(self, state):
        numbering = self.store.numbering(self.context.slug)
        form = self._identification_form()
        static.need_form(form)
        html = (
            state["html"]
            if "html" in state
            else form.render({"last": numbering["last"]})
        )
        errors = "".join(
            f'<div class="alert alert-danger">{escape(e)}</div>'
            for e in state.get("errors", [])
        )
        facts = (
            f'<dl class="row small text-muted">'
            f'<dt class="col-sm-3">Highest used</dt>'
            f'<dd class="col-sm-9">{numbering["highest"]}</dd>'
            f'<dt class="col-sm-3">Last given out</dt>'
            f'<dd class="col-sm-9">{numbering["given"]}</dd>'
            f'<dt class="col-sm-3">Reserved up to</dt>'
            f'<dd class="col-sm-9">{numbering["reserved"]}</dd>'
            f'<dt class="col-sm-3">Next number</dt>'
            f'<dd class="col-sm-9">{numbering["next"]}</dd>'
            "</dl>"
        )
        return errors + html + facts

    def post_type_identification(self, _rest):
        form = self._identification_form()
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        try:
            self.store.reserve_ids(self.context.slug, data["last"])
        except ValueError as err:
            return {"html": form.render(data), "errors": [str(err)]}
        flash(self.request, "Saved.")
        return HTTPFound(self.action("identification"))

    # The jobs about the records of the type. A button starts one with the fields of its
    # own form
    # (`jobs.js`); the validate job also asks for the schema file to check them against.
    TYPE_JOBS = ("validate", "export", "rewrite", "purge_slugs")

    def pane_jobs(self, state):
        request, res_type = self.request, self.context
        jobs_node = res_type.repository["jobs"]
        items = []
        for job in all_jobs(request.registry):
            if job.name not in self.TYPE_JOBS or not request.has_permission(
                job.start_permission, jobs_node
            ):
                continue
            files = (
                [(f, f) for f in assets.schemas_of(res_type.assets)]
                if (job.name == "validate" and res_type.assets)
                else []
            )
            if files:
                files.insert(0, ("", "the schema the type is on"))
            items.append(
                {
                    "name": job.name,
                    "title": job.title,
                    "description": job.description,
                    "url": request.resource_url(
                        jobs_node, "start", query={"job": job.name}
                    ),
                    "schemas": files,
                    "fields": "res_type,schema" if files else "res_type",
                }
            )
        items.sort(key=lambda item: self.TYPE_JOBS.index(item["name"]))
        return fragment(
            request,
            "gracia:assets/templates/type_tab_jobs.pt",
            jobs=items,
            res_type=res_type.slug,
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    def post_type_settings(self, _rest):
        record = self._record()
        form = self._form(record)
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        problems = record.check(data)
        if problems:
            return {"html": form.render(data), "errors": problems}
        record.apply(data)
        record.save()
        flash(self.request, "Saved.")
        return HTTPFound(self.action("settings"))
