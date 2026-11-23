from html import escape

import colander
import deform
import deform.widget

from gracia import uri
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal

RESERVED = (
    "add",
    "manage",
)  # names of the views of a type: no record can be called that


class SlugWidget(deform.widget.TextInputWidget):
    def __init__(self, options, **kw):
        super().__init__(attributes={"autocomplete": "off"}, **kw)
        self.options = list(options)

    def serialize(self, field, cstruct, **kw):
        field_html = super().serialize(field, cstruct, **kw)
        if not self.options:
            return field_html
        shown = (cstruct or "") if cstruct is not colander.null else ""
        options = "".join(
            f'<option value="{escape(name)}"{" selected" if name == shown else ""}>'
            f"{escape(name)}{' (now)' if name == shown else ''}</option>"
            for name in self.options
        )
        return (
            f'<div class="input-group">{field_html}'
            f'<select class="form-select flex-grow-0 w-auto" id="slug-options" '
            f'aria-label="Previous slugs" '
            f"onchange=\"if(this.value)document.getElementById('{field.oid}').value=this.value\">"
            f'<option value="">Previous slugs\u2026</option>{options}</select></div>'
        )


@predicate(uri.SLUG)
class SlugPredicate(Predicate):
    """`gracia:slug`: the name of a record in the address of its type, made on save."""

    def schema_node(self, record, request, schema_node=None):
        if schema_node is None or getattr(schema_node, "depth", 0) != 0:
            return (
                schema_node  # the slug of a type is a plain field, it is of the content
            )
        old = (
            request.store.slugs_of(record.uri)
            if request is not None and record.uri
            else []
        )
        return colander.SchemaNode(
            colander.String(),
            name="slug",
            title="Slug",
            description=(
                "The name of the record in its address (john-doe); empty, it is made "
                "of the label. "
                "The names it has had are in the list, to use one of them again"
            ),
            missing="",
            validator=colander.Length(max=200),
            widget=SlugWidget(old),
        )

    def on_write_sql(self, context: HookContext) -> list[str]:
        flat, of_type = context.flat, context.of_type
        slug, label = literal(uri.SLUG), literal(uri.LABEL)
        rec_type = literal(context.type.slug)  # ty: ignore[missing-attribute]
        reserved = ", ".join(literal(name) for name in RESERVED)
        work = f"{flat}_slug"
        return [
            f"""CREATE OR REPLACE TABLE {work} AS
                WITH recs AS (SELECT DISTINCT s FROM {flat} WHERE {of_type}),
                stated AS (SELECT s, max(lit) AS slug FROM {flat}
                           WHERE {of_type} AND len(loc) = 0 AND p = {slug} GROUP BY s),
                labels AS (SELECT s, max(lit) AS label FROM {flat}
                           WHERE {of_type} AND len(loc) = 0 AND p = {label} GROUP BY s),
                want AS (
                    SELECT r.s, slugify(coalesce(nullif(trim(st.slug), ''), l.label)) AS
                      base
                    FROM recs r LEFT JOIN stated st ON st.s = r.s LEFT JOIN labels l ON
                      l.s = r.s),
                wanted AS (SELECT s, base FROM want
                           WHERE base <> '' AND NOT regexp_matches(base, '^[0-9]+$')),
                owned AS (SELECT record_uri AS s, slug FROM slugs
                          WHERE record_uri IN (SELECT s FROM recs)),
                keep AS (SELECT w.s, w.base AS slug FROM wanted w
                         JOIN owned o ON o.s = w.s AND o.slug = w.base),
                need AS (SELECT s, base FROM wanted WHERE s NOT IN (SELECT s FROM
                  keep)),
                taken AS (
                    SELECT n.base, g.slug FROM (SELECT DISTINCT base FROM need) n
                    JOIN slugs g ON g.rec_type = {rec_type}
                                AND (g.slug = n.base OR starts_with(g.slug, n.base ||
                                  '-'))
                    UNION SELECT base, base FROM need WHERE base IN ({reserved})),
                fam AS (SELECT n.base, count(*) AS needs,
                               (SELECT count(*) FROM taken t WHERE t.base = n.base) AS
                                 taken_n
                        FROM need n GROUP BY n.base),
                cands AS (SELECT f.base, k.k,
                                 CASE WHEN k.k = 1 THEN f.base ELSE f.base || '-' || k.k
                                   END AS slug
                          FROM fam f, LATERAL (SELECT unnest(range(1, f.needs +
                            f.taken_n + 2)) AS k) k),
                free AS (SELECT c.base, c.slug,
                                row_number() OVER (PARTITION BY c.base ORDER BY c.k) AS
                                  rn
                         FROM cands c
                         WHERE NOT EXISTS (SELECT 1 FROM taken t
                                           WHERE t.base = c.base AND t.slug = c.slug)),
                ranked AS (SELECT n.s, n.base,
                                  row_number() OVER (PARTITION BY n.base
                                      ORDER BY CAST(split_part(n.s, ':', 3) AS BIGINT))
                                        AS rn
                           FROM need n)
                SELECT s, slug FROM keep
                UNION ALL SELECT r.s, f.slug FROM ranked r JOIN free f
                          ON f.base = r.base AND f.rn = r.rn""",
            f"DELETE FROM {flat} WHERE {of_type} AND len(loc) = 0 AND p = {slug}",
            f"""INSERT INTO {flat}
                SELECT s, []::VARCHAR[], {slug}, NULL, slug, NULL, NULL FROM {work}""",
            f"DROP TABLE IF EXISTS {work}",
        ]
