<?xml version="1.0" encoding="UTF-8"?>
<!--
  The doc of a work (`gracia:docTemplate` of the type): the doc of every record (doc-base), the fields a
  person searches works by, and the work as CSL JSON, which is what a citation is made of (citeproc).

  The input is what the base says it is: the RDF/XML of the records of the batch and of what they are made
  of. For a work that is the people of its author list, the organization that publishes it, the series it
  is part of and the people and organizations of its contributors: they are looked up by the resource uri
  that the work refers to them by.

  The fields (simple names, for the search index; one with several values is an array):

    year              integer   the year of schema:datePublished
    author_name       text      the authors, "Given Family", in the order of the list
    contributor_name  text      who has a role in it (an editor, a supervisor, a funder)
    journal_name      text      the periodical or the newspaper it is in
    series_name       text      the series of books, or of reports, it is in
    publisher_name    text      the publisher (of a thesis without one, the degree grantor)
    work_type         text      its class: Thesis, ScholarlyArticle, Book, ...
    csl_type          text      the type of the CSL item: thesis, article-journal, book, ...
    language          text      schema:inLanguage
    keyword           text      the keywords, one each
    doi, isbn, issn   text      its identifiers (the ISSN is the one of the journal or the series)
    volume, issue     text
    csl               text      the work as an item of CSL JSON (the schema in data/csl-data.json), a JSON string kept whole (raw: one term)

  What a work does not have is not in them, and not in the CSL item, which has an id and a type at least.
-->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:map="http://www.w3.org/2005/xpath-functions/map"
    xmlns:array="http://www.w3.org/2005/xpath-functions/array"
    xmlns:doc="urn:gracia:doc"
    xmlns:w="urn:gracia:doc:work"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:g="https://gracia.pub/ns/"
    exclude-result-prefixes="#all">

  <xsl:import href="type:type/doc/doc-base-01.xsl"/>

  <xsl:variable name="w:schema" select="'https://schema.org/'"/>
  <!-- the classes of the series a work can be part of -->
  <xsl:variable name="w:periodical" select="$w:schema || 'Periodical'"/>
  <xsl:variable name="w:newspaper" select="$w:schema || 'Newspaper'"/>
  <xsl:variable name="w:series" select="($w:schema || 'BookSeries', $w:schema || 'CreativeWorkSeries')"/>

  <!-- the record that has a resource uri (a work refers to what it is about by it), if the input has it -->
  <xsl:key name="w:record-of" match="/rdf:RDF/*[g:resource_uri]" use="string(g:resource_uri/@rdf:resource)"/>

  <xsl:function name="w:record" as="element()?">
    <xsl:param name="uri" as="xs:string"/>
    <xsl:sequence select="key('w:record-of', $uri, $root)[1]"/>
  </xsl:function>

  <!-- the last part of an iri: the name of a class, or of a role -->
  <xsl:function name="w:local" as="xs:string">
    <xsl:param name="iri" as="xs:string?"/>
    <xsl:sequence select="replace(string($iri), '^.*[/#]', '')"/>
  </xsl:function>

  <!-- the resource of a record, if there is a record (doc:resource wants one) -->
  <xsl:function name="w:thing" as="element()?">
    <xsl:param name="record" as="element()?"/>
    <xsl:sequence select="if (exists($record)) then doc:resource($record) else ()"/>
  </xsl:function>

  <!-- the values a property of schema.org has in a subject -->
  <xsl:function name="w:prop" as="element()*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:param name="name" as="xs:string"/>
    <xsl:sequence select="$subject/*[namespace-uri(.) = $w:schema and local-name(.) = $name]"/>
  </xsl:function>

  <!-- the plain texts of a property, trimmed, without what is empty (the first of them: w:text) -->
  <xsl:function name="w:texts" as="xs:string*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:param name="name" as="xs:string"/>
    <xsl:sequence select="(w:prop($subject, $name)[not(@rdf:resource or @rdf:nodeID)] ! normalize-space(.))[. != '']"/>
  </xsl:function>

  <xsl:function name="w:text" as="xs:string?">
    <xsl:param name="subject" as="element()?"/>
    <xsl:param name="name" as="xs:string"/>
    <xsl:sequence select="w:texts($subject, $name)[1]"/>
  </xsl:function>

  <!-- the classes of a resource -->
  <xsl:function name="w:classes" as="xs:string*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:sequence select="if (exists($subject)) then array:flatten(doc:types($subject)) ! string(.) else ()"/>
  </xsl:function>

  <!-- the values of a list (rdf:first and rdf:rest, the nodes of it have names that begin with that of the
       head): in its order; one that goes round is left where it came back to the start -->
  <xsl:function name="w:list-items" as="xs:string*">
    <xsl:param name="head" as="xs:string"/>
    <xsl:param name="seen" as="xs:string*"/>
    <xsl:variable name="cell" select="key('subject', $head, $root)[1]"/>
    <xsl:if test="exists($cell) and not($head = $seen)">
      <xsl:sequence select="$cell/rdf:first/@rdf:resource ! string(.)"/>
      <xsl:sequence select="$cell/rdf:rest/@rdf:nodeID ! w:list-items(string(.), ($seen, $head))"/>
    </xsl:if>
  </xsl:function>

  <!-- the records that a property has: its values are a list (an author list), in its order, or the
       resource uris of records; a record the input does not have is left out -->
  <xsl:function name="w:records" as="element()*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:param name="name" as="xs:string"/>
    <xsl:for-each select="w:prop($subject, $name)">
      <xsl:sequence select="if (@rdf:nodeID) then w:list-items(string(@rdf:nodeID), ()) ! w:record(.)
                            else if (@rdf:resource) then w:record(string(@rdf:resource))
                            else ()"/>
    </xsl:for-each>
  </xsl:function>

  <!-- the name of the person (or organization) of a record, as CSL has it: family and given, or the whole
       name as one (`literal`); none for a record that has no name -->
  <xsl:function name="w:csl-name" as="map(*)?">
    <xsl:param name="record" as="element()"/>
    <xsl:variable name="resource" select="doc:resource($record)"/>
    <xsl:variable name="family" select="w:text($resource, 'familyName')"/>
    <xsl:variable name="given" select="w:text($resource, 'givenName')"/>
    <xsl:variable name="whole" select="(w:text($resource, 'name'), normalize-space($record/g:label[1]))[. != ''][1]"/>
    <xsl:sequence select="
        if (exists($family)) then map:merge((map { 'family': $family }, if (exists($given)) then map { 'given': $given } else ()))
        else if (exists($whole)) then map { 'literal': $whole }
        else ()"/>
  </xsl:function>

  <!-- a name as people say it: given and family -->
  <xsl:function name="w:said" as="xs:string?">
    <xsl:param name="name" as="map(*)?"/>
    <xsl:sequence select="
        if (empty($name)) then ()
        else if (exists($name?family)) then string-join(($name?given, $name?family), ' ')
        else $name?literal"/>
  </xsl:function>

  <!-- the date of a work as CSL has it: date-parts of the year, the year and month, or the date -->
  <xsl:function name="w:date-parts" as="array(*)?">
    <xsl:param name="date" as="xs:string?"/>
    <xsl:sequence select="if (matches(string($date), '^\d{4}(-\d{2}(-\d{2})?)?$'))
                          then array { array { tokenize($date, '-') ! xs:integer(.) } }
                          else ()"/>
  </xsl:function>

  <!-- the values of the identifiers of a kind (DOI, ISBN, ISSN) that a resource has -->
  <xsl:function name="w:identifiers" as="xs:string*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:param name="kind" as="xs:string"/>
    <xsl:for-each select="w:prop($subject, 'identifier')/@rdf:nodeID">
      <xsl:variable name="node" select="key('subject', string(.), $root)[1]"/>
      <xsl:if test="w:text($node, 'propertyID') = $kind">
        <xsl:sequence select="w:text($node, 'value')"/>
      </xsl:if>
    </xsl:for-each>
  </xsl:function>

  <!-- the series that a work is part of (a periodical, a newspaper, a series of books or reports): its
       record; a collection of the repository is not one a citation names -->
  <xsl:function name="w:container" as="element()?">
    <xsl:param name="subject" as="element()?"/>
    <xsl:sequence select="(w:records($subject, 'isPartOf')[w:classes(w:thing(.)) = ($w:periodical, $w:newspaper, $w:series)])[1]"/>
  </xsl:function>

  <!-- who has a role in the work: the role (its last part: Editor, Funder) and the name of who it is for -->
  <xsl:function name="w:contributors" as="map(*)*">
    <xsl:param name="subject" as="element()?"/>
    <xsl:variable name="found" as="map(*)*">
      <xsl:for-each select="w:prop($subject, 'contributor')/@rdf:nodeID">
        <xsl:variable name="role" select="key('subject', string(.), $root)[1]"/>
        <xsl:variable name="who" select="w:prop($role, 'contributor')/@rdf:resource ! w:record(string(.))"/>
        <xsl:variable name="name" select="$who ! w:csl-name(.)"/>
        <xsl:if test="exists($name)">
          <xsl:sequence select="map { 'role': w:local(string(w:prop($role, 'roleName')/@rdf:resource)), 'name': $name[1] }"/>
        </xsl:if>
      </xsl:for-each>
    </xsl:variable>
    <xsl:sequence select="sort($found, (), function($c) { w:said($c?name) || '|' || $c?role })"/>
  </xsl:function>

  <!-- an array of the values (texts, or the names of people as maps), or nothing when there are
       none (a field with no value is left out) -->
  <xsl:function name="w:many" as="array(*)?">
    <xsl:param name="values" as="item()*"/>
    <xsl:sequence select="if (exists($values)) then array { $values } else ()"/>
  </xsl:function>

  <!-- the type of the CSL item: of the class of the work, and of what it is part of -->
  <xsl:function name="w:csl-type" as="xs:string">
    <xsl:param name="class" as="xs:string?"/>
    <xsl:param name="container" as="xs:string*"/>
    <xsl:sequence select="
        if ($class = 'Thesis') then 'thesis'
        else if ($class = 'Book') then 'book'
        else if ($class = 'Report') then 'report'
        else if ($class = 'Dataset') then 'dataset'
        else if ($class = 'SoftwareSourceCode') then 'software'
        else if ($class = 'Chapter') then 'chapter'
        else if ($class = ('ScholarlyArticle', 'Article')) then
            (if ($container = $w:newspaper) then 'article-newspaper'
             else if ($container = $w:periodical) then (if ($class = 'Article') then 'article-magazine' else 'article-journal')
             else 'article')
        else 'document'"/>
  </xsl:function>

  <!-- the fields this type adds, for the schema mode (see the base) -->
  <xsl:template match="/types/type[@slug = 'work']" mode="schema" as="map(*)*">
    <xsl:variable name="base" as="map(*)*"><xsl:next-match/></xsl:variable>
    <xsl:sequence select="(
        $base,
        doc:field('year',             'integer', map { 'fast': true() }),
        doc:field('author_name',      'text'),
        doc:field('contributor_name', 'text'),
        doc:field('journal_name',     'text'),
        doc:field('series_name',      'text'),
        doc:field('publisher_name',   'text'),
        doc:field('work_type',        'text',    map { 'tokenizer': 'raw' }),
        doc:field('csl_type',         'text',    map { 'tokenizer': 'raw' }),
        doc:field('language',         'text',    map { 'tokenizer': 'raw' }),
        doc:field('keyword',          'text'),
        doc:field('doi',              'text',    map { 'tokenizer': 'raw' }),
        doc:field('isbn',             'text',    map { 'tokenizer': 'raw' }),
        doc:field('issn',             'text',    map { 'tokenizer': 'raw' }),
        doc:field('volume',           'text',    map { 'tokenizer': 'raw' }),
        doc:field('issue',            'text',    map { 'tokenizer': 'raw' }),
        doc:field('csl',              'text',    map { 'tokenizer': 'raw' })
    )"/>
  </xsl:template>

  <xsl:template match="/rdf:RDF/*[starts-with(@rdf:about, 'rec:work:')]" mode="doc" as="map(*)">
    <xsl:variable name="base" as="map(*)"><xsl:next-match/></xsl:variable>
    <xsl:variable name="work" select="doc:resource(.)"/>

    <xsl:variable name="class" select="w:local(w:classes($work)[1])[. != '']"/>
    <xsl:variable name="authors" select="w:records($work, 'author') ! w:csl-name(.)" as="map(*)*"/>
    <xsl:variable name="contributors" select="w:contributors($work)" as="map(*)*"/>
    <xsl:variable name="editors" select="$contributors[?role = 'Editor']?name" as="map(*)*"/>
    <xsl:variable name="container" select="w:container($work)"/>
    <xsl:variable name="container-resource" select="w:thing($container)"/>
    <xsl:variable name="container-class" select="w:classes($container-resource)[. = ($w:periodical, $w:newspaper, $w:series)][1]"/>
    <xsl:variable name="container-name" select="w:text($container-resource, 'name')"/>
    <xsl:variable name="in-journal" select="$container-class = ($w:periodical, $w:newspaper)"/>
    <xsl:variable name="publisher" select="w:records($work, 'publisher')[1]"/>
    <xsl:variable name="grantor" select="($contributors[?role = 'DegreeGrantor'])[1]"/>
    <xsl:variable name="publisher-name" select="(w:said($publisher ! w:csl-name(.)), if ($class = 'Thesis') then w:said($grantor?name) else ())[1]"/>
    <xsl:variable name="place" select="w:text(key('subject', string(w:prop(w:thing($publisher), 'address')/@rdf:nodeID), $root)[1], 'addressLocality')"/>
    <xsl:variable name="date" select="w:text($work, 'datePublished')"/>
    <xsl:variable name="year" select="if (exists(w:date-parts($date))) then xs:integer(substring($date, 1, 4)) else ()"/>
    <xsl:variable name="dois" select="distinct-values(w:identifiers($work, 'DOI'))"/>
    <xsl:variable name="isbns" select="distinct-values(w:identifiers($work, 'ISBN'))"/>
    <xsl:variable name="issns" select="distinct-values(w:identifiers($container-resource, 'ISSN'))"/>
    <xsl:variable name="keywords" select="sort(distinct-values(w:texts($work, 'keywords') ! tokenize(., ',') ! normalize-space(.)[. != '']))"/>
    <xsl:variable name="csl-type" select="w:csl-type($class, $container-class)"/>
    <xsl:variable name="pages" select="(w:text($work, 'pageStart'), w:text($work, 'pageEnd'))"/>
    <xsl:variable name="language" select="w:text($work, 'inLanguage')"/>
    <xsl:variable name="id" select="(string(g:resource_uri/@rdf:resource)[. != ''], string(@rdf:about))[1]"/>

    <!-- the work as an item of CSL JSON -->
    <xsl:variable name="csl" select="doc:fields(map {
        'id':              $id,
        'type':            $csl-type,
        'title':           w:text($work, 'name'),
        'author':          w:many($authors),
        'editor':          w:many($editors),
        'issued':          w:date-parts($date) ! map { 'date-parts': . },
        'container-title': if ($in-journal) then $container-name else (),
        'collection-title': if (exists($container-class) and not($in-journal)) then $container-name else (),
        'volume':          w:text($work, 'volumeNumber'),
        'issue':           w:text($work, 'issueNumber'),
        'page':            if (exists($pages)) then string-join(distinct-values($pages), '-') else (),
        'publisher':       $publisher-name,
        'publisher-place': $place,
        'genre':           w:text($work, 'inSupportOf'),
        'language':        $language,
        'abstract':        w:text($work, 'abstract'),
        'keyword':         if (exists($keywords)) then string-join($keywords, ', ') else (),
        'DOI':             $dois[1],
        'ISBN':            $isbns[1],
        'ISSN':            $issns[1],
        'URL':             (w:prop($work, 'url')/@rdf:resource ! string(.))[1]
    })" as="map(*)"/>

    <xsl:sequence select="map:merge(($base, doc:fields(map {
        'year':             $year,
        'author_name':      w:many($authors ! w:said(.)),
        'contributor_name': w:many($contributors ! w:said(?name)),
        'journal_name':     if ($in-journal) then $container-name else (),
        'series_name':      if (exists($container-class) and not($in-journal)) then $container-name else (),
        'publisher_name':   $publisher-name,
        'work_type':        $class,
        'csl_type':         $csl-type,
        'language':         $language,
        'keyword':          w:many($keywords),
        'doi':              w:many($dois),
        'isbn':             w:many($isbns),
        'issn':             w:many($issns),
        'volume':           w:text($work, 'volumeNumber'),
        'issue':            w:text($work, 'issueNumber'),
        'csl':              serialize($csl, map { 'method': 'json', 'indent': false() })
    })))"/>
  </xsl:template>
</xsl:stylesheet>
