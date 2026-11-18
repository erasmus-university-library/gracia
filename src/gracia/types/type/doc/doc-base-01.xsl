<?xml version="1.0" encoding="UTF-8"?>
<!--
  The base of every doc: flat RDF/XML in, JSON out (XSLT 3.0). A doc is the JSON object a record is
  known by to what follows the data (the search index, the /doc view of a record).

  The input is the RDF/XML of the records of a batch and of what they are made of (the graph cache
  writes the same for one record), so a record can be asked about its references too. The parameter:

    records     JSON, an object: the uri of each record to make a doc of -> its record id

  The output is an array with one object for each record, by record id; for a record that cannot be made
  `{"record": <uri>, "error": <why>}`, so that one record does not stop the batch.

  How it is made: the root template applies the templates of the mode "doc" to the node of each record.
  This stylesheet has the template for every record (`rec:`): the fields every doc has. A type adds fields
  in a stylesheet of its own (`doc/doc-<profile>-<revision>.xsl`, named by its `gracia:docTemplate`) that
  imports this one, with a template for the records of the type, that is, for the uri that starts with
  `rec:<slug>:`, which asks for the doc this one makes (`xsl:next-match`) and adds to it:

    <xsl:import href="type:type/doc/doc-base-01.xsl"/>
    <xsl:template match="/rdf:RDF/*[starts-with(@rdf:about, 'rec:person:')]" mode="doc" as="map(*)">
      <xsl:variable name="base" as="map(*)"><xsl:next-match/></xsl:variable>
      <xsl:sequence select="map:merge(($base, map { 'given_name': ... }))"/>
    </xsl:template>

  The schema mode: the same stylesheets say which fields their docs have, so that the index schema can be
  compared with what the docs can offer. With the parameter `schema` set to `true` the input is not records
  but `<types><type slug="person"/>...</types>`, and the output is an array with one object for each field
  there is: `name`, `type` (integer, text, date, boolean or json), `stored`, `indexed`, `fast`, `tokenizer`
  (what `gracia.search.Field` has) and `types`, the slugs of the types whose docs have it (a field that two
  types describe differently is `conflict`). The templates are those of the mode "schema", dispatched the
  way the docs are: this stylesheet has the fields every doc has, and a type's template for
  `/types/type[@slug='person']` asks for them with `xsl:next-match` and adds its own, made with `doc:field`:

    <xsl:template match="/types/type[@slug='person']" mode="schema" as="map(*)*">
      <xsl:variable name="base" as="map(*)*"><xsl:next-match/></xsl:variable>
      <xsl:sequence select="($base, doc:field('given_name', 'text'))"/>
    </xsl:template>

  Dispatch is on the record's type and nothing else, so the stylesheets of all the types can be imported by
  one (what the repository's doc stylesheet is) without a template of one type seeing a record of another.
  Fields a schema does not have are dropped by the search index, so a type may make more than the index
  uses.
-->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:map="http://www.w3.org/2005/xpath-functions/map"
    xmlns:err="http://www.w3.org/2005/xqt-errors"
    xmlns:doc="urn:gracia:doc"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:g="https://gracia.pub/ns/"
    exclude-result-prefixes="#all">

  <xsl:output method="json" indent="yes"/>
  <xsl:param name="records" as="xs:string" select="'{}'"/>
  <xsl:param name="schema" as="xs:string" select="'false'"/>

  <!-- every subject is a child of rdf:RDF, a uri or a blank node; blank nodes are named b<record>_<n> -->
  <xsl:key name="subject" match="/rdf:RDF/*" use="(@rdf:about, @rdf:nodeID)"/>

  <xsl:variable name="root" select="/"/>
  <xsl:variable name="ids" select="parse-json($records)" as="map(*)"/>

  <!-- the resource of a record: the blank node `gracia:resource` points at -->
  <xsl:function name="doc:resource" as="element()?">
    <xsl:param name="record" as="element()"/>
    <xsl:sequence select="key('subject', string($record/g:resource/@rdf:nodeID), $root)[1]"/>
  </xsl:function>

  <!-- the literals of a subject and of the blank nodes below it, the words a person would search for
       (never a credential; RDF has no order: callers sort them, so that the same record always gives the
       same doc) -->
  <xsl:function name="doc:literals" as="xs:string*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:for-each select="$subject/*[not(self::g:credentials)]">
      <xsl:choose>
        <xsl:when test="@rdf:nodeID">
          <xsl:sequence select="doc:literals(key('subject', string(@rdf:nodeID), $root))"/>
        </xsl:when>
        <xsl:when test="@rdf:resource"/>
        <xsl:when test="normalize-space(.)">
          <xsl:sequence select="normalize-space(.)"/>
        </xsl:when>
      </xsl:choose>
    </xsl:for-each>
  </xsl:function>

  <!-- the text values of a property of the resource of a record, sorted and distinct -->
  <xsl:function name="doc:values" as="xs:string*">
    <xsl:param name="record" as="element()"/>
    <xsl:param name="property" as="xs:QName"/>
    <xsl:sequence select="sort(distinct-values(
        for $p in doc:resource($record)/*[node-name(.) = $property][not(@rdf:resource or @rdf:nodeID)]
        return normalize-space($p)[. != '']))"/>
  </xsl:function>

  <!-- the fields that have a value: an empty string is no value -->
  <xsl:function name="doc:fields" as="map(*)">
    <xsl:param name="all" as="map(*)"/>
    <xsl:sequence select="map:merge(
        for $key in map:keys($all)
        return if (empty($all($key)) or ($all($key) instance of xs:string and $all($key) = ''))
               then () else map:entry($key, $all($key)))"/>
  </xsl:function>

  <!-- a field of the schema: a name and a type, and what else is not the default (`stored` and `indexed`
       true, `fast` false, the tokenizer `default`) -->
  <xsl:function name="doc:field" as="map(*)">
    <xsl:param name="name" as="xs:string"/>
    <xsl:param name="type" as="xs:string"/>
    <xsl:sequence select="doc:field($name, $type, map {})"/>
  </xsl:function>

  <xsl:function name="doc:field" as="map(*)">
    <xsl:param name="name" as="xs:string"/>
    <xsl:param name="type" as="xs:string"/>
    <xsl:param name="options" as="map(*)"/>
    <xsl:sequence select="map:merge((
        map { 'name': $name, 'type': $type, 'stored': true(), 'indexed': true(), 'fast': false(), 'tokenizer': 'default' },
        $options), map { 'duplicates': 'use-last' })"/>
  </xsl:function>

  <!-- the fields of the doc every record has (the template of the doc below makes them) -->
  <xsl:template match="/types/type" mode="schema" as="map(*)*" priority="-1">
    <xsl:sequence select="(
        doc:field('record_id',    'integer', map { 'fast': true() }),
        doc:field('record',       'text',    map { 'tokenizer': 'raw' }),
        doc:field('type',         'text',    map { 'tokenizer': 'raw' }),
        doc:field('slug',         'text',    map { 'tokenizer': 'raw' }),
        doc:field('label',        'text'),
        doc:field('text',         'text',    map { 'stored': false() }),
        doc:field('resource_uri', 'text',    map { 'tokenizer': 'raw' }),
        doc:field('published',    'boolean', map { 'fast': true() }),
        doc:field('modified',     'date',    map { 'fast': true() })
    )"/>
  </xsl:template>

  <!-- the doc every record has -->
  <xsl:template match="/rdf:RDF/*[starts-with(@rdf:about, 'rec:')]" mode="doc" as="map(*)" priority="-1">
    <xsl:variable name="uri" select="string(@rdf:about)"/>
    <xsl:sequence select="doc:fields(map {
        'record_id':    xs:integer($ids($uri)),
        'record':       $uri,
        'type':         replace($uri, '^rec:([^:]+):.*$', '$1'),
        'slug':         string(g:slug[1]),
        'label':        string(g:label[1]),
        'text':         string-join(sort(distinct-values(doc:literals(doc:resource(.)))), ' '),
        'resource_uri': string(g:resource_uri/@rdf:resource),
        'published':    not(string(g:is_published[1]) = ('false', '0')),
        'modified':     string(g:modified_at[1])
    })"/>
  </xsl:template>

  <xsl:template match="/">
    <xsl:choose>
      <xsl:when test="$schema = 'true'"><xsl:call-template name="doc:schema"/></xsl:when>
      <xsl:otherwise><xsl:call-template name="doc:docs"/></xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <!-- the schema mode: every field of every type, once, with the types that have it -->
  <xsl:template name="doc:schema">
    <xsl:variable name="found" as="map(*)*">
      <xsl:for-each select="/types/type">
        <xsl:variable name="slug" select="string(@slug)"/>
        <xsl:variable name="fields" as="map(*)*"><xsl:apply-templates select="." mode="schema"/></xsl:variable>
        <xsl:sequence select="for $field in $fields return map { 'slug': $slug, 'field': $field }"/>
      </xsl:for-each>
    </xsl:variable>
    <xsl:sequence select="array {
        for $name in distinct-values($found ! ?field?name)
        return
          let $mine := $found[?field?name = $name],
              $first := $mine[1]?field
          return map:merge((
              $first,
              map { 'types': array { $mine ! ?slug } },
              if (every $m in $mine satisfies deep-equal($m?field, $first)) then () else map { 'conflict': true() }
          ), map { 'duplicates': 'use-last' })
    }"/>
  </xsl:template>

  <xsl:template name="doc:docs">
    <xsl:variable name="docs" as="map(*)*">
      <xsl:for-each select="sort(map:keys($ids), (), function($uri) { $ids($uri) })">
        <xsl:variable name="uri" select="."/>
        <xsl:try>
          <xsl:variable name="record" select="key('subject', $uri, $root)[1]"/>
          <xsl:if test="empty($record)">
            <xsl:sequence select="error(xs:QName('doc:missing'), 'the record is not in the input')"/>
          </xsl:if>
          <xsl:apply-templates select="$record" mode="doc"/>
          <xsl:catch>
            <xsl:sequence select="map { 'record': $uri, 'error': string($err:description) }"/>
          </xsl:catch>
        </xsl:try>
      </xsl:for-each>
    </xsl:variable>
    <xsl:sequence select="array { $docs }"/>
  </xsl:template>
</xsl:stylesheet>
