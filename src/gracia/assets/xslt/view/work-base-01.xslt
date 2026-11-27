<?xml version="1.0" encoding="UTF-8"?>
<!-- The base of the page of a work (the type `work`), the way a repository shows a publication: a
     header with the breadcrumb, the authors, the date and the title; then a Publication section
     (the abstract), Additional Metadata (label and value pairs), more sections a profile may
     add, and the files. A profile of the work (`work-<profile>-<revision>.xslt`, named by the
     `gracia:viewTemplate` of the type) imports this one and fills in the slots, which are
     templates for the request in a mode of their own:

       work-title        the title (the text); the heading of the page when there is none
       work-authors      the authors, as inline markup
       work-date         the date, as inline markup
       work-description  the paragraphs of the abstract
       work-metadata     the rows of the metadata: call `work:row` for each
       work-sections     whole sections (`<section>`) between the metadata and the files
       work-files        the files, as markup

     A slot that makes nothing hides its section. What is here when a profile does not say: the
     description and the key information of the shape of the type, as the default page of a
     record (view/record-minimal-01.xslt) shows them. It is a stylesheet of the page, put in the layout of
     the repository (layout.xslt), after the helpers of rdf.xslt. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:f="https://gracia.pub/ns/functions" xmlns:work="https://gracia.pub/ns/work"
    exclude-result-prefixes="#all">

  <!-- the record, its resource (the blank node with the data) and the address of the repository -->
  <xsl:variable name="work:record" select="f:record(/request/context/@record_uri, /)"/>
  <xsl:variable name="work:resource" select="f:resource($work:record)"/>
  <xsl:variable name="work:repository" select="string(/request/context/node[@kind = 'Repository']/@url)"/>

  <!-- the values of a property of the resource -->
  <xsl:function name="work:objects" as="element()*">
    <xsl:param name="path" as="xs:string"/>
    <xsl:sequence select="f:objects($work:resource, $path)"/>
  </xsl:function>

  <!-- a row of the metadata: a label and its values (a link where a value is one) or its texts -->
  <xsl:template name="work:row">
    <xsl:param name="label" as="xs:string"/>
    <xsl:param name="values" as="element()*" select="()"/>
    <xsl:param name="texts" as="xs:string*" select="()"/>
    <xsl:if test="exists($values) or exists($texts)">
      <dt class="col-sm-3"><xsl:value-of select="$label"/></dt>
      <dd class="col-sm-9">
        <xsl:for-each select="$values">
          <xsl:variable name="link" select="f:link(., $work:repository)"/>
          <div>
            <xsl:choose>
              <xsl:when test="exists($link)"><a href="{$link}"><xsl:value-of select="f:name(.)"/></a></xsl:when>
              <xsl:otherwise><xsl:value-of select="f:name(.)"/></xsl:otherwise>
            </xsl:choose>
          </div>
        </xsl:for-each>
        <xsl:for-each select="$texts"><div><xsl:value-of select="."/></div></xsl:for-each>
      </dd>
    </xsl:if>
  </xsl:template>

  <!-- the defaults of the slots -->
  <xsl:template match="/request" mode="work-title">
    <xsl:value-of select="view/param[@name = 'heading']"/>
  </xsl:template>

  <xsl:template match="/request" mode="work-authors"/>

  <xsl:template match="/request" mode="work-date"/>

  <xsl:template match="/request" mode="work-description">
    <xsl:variable name="path" select="(for $p in properties/property[@role = 'description']/@path
        return if (exists(work:objects($p))) then $p else ())[1]"/>
    <xsl:for-each select="work:objects($path)">
      <p><xsl:value-of select="f:text(.)"/></p>
    </xsl:for-each>
  </xsl:template>

  <xsl:template match="/request" mode="work-metadata">
    <xsl:variable name="request" select="."/>
    <xsl:for-each select="distinct-values(properties/property[@role = 'key_info']/@path)">
      <xsl:call-template name="work:row">
        <xsl:with-param name="label" select="string($request/properties/property[@path = current()][1]/@name)"/>
        <xsl:with-param name="values" select="sort(work:objects(.), (), f:text#1)"/>
      </xsl:call-template>
    </xsl:for-each>
  </xsl:template>

  <xsl:template match="/request" mode="work-sections"/>

  <xsl:template match="/request" mode="work-files"/>

  <!-- the page -->
  <xsl:template match="/request" mode="title">
    <xsl:apply-templates select="." mode="work-title"/>
  </xsl:template>

  <xsl:template match="/request" mode="header">
    <xsl:variable name="authors"><xsl:apply-templates select="." mode="work-authors"/></xsl:variable>
    <xsl:variable name="date"><xsl:apply-templates select="." mode="work-date"/></xsl:variable>
    <nav aria-label="breadcrumb">
      <ol class="breadcrumb small mb-1">
        <xsl:for-each select="context/node[@kind = ('Repository', 'Type')]">
          <li class="breadcrumb-item"><a href="{@url}"><xsl:value-of select="@name"/></a></li>
        </xsl:for-each>
      </ol>
    </nav>
    <xsl:if test="exists($authors/node()) or exists($date/node())">
      <div class="text-muted work-byline">
        <xsl:copy-of select="$authors/node()"/>
        <xsl:if test="exists($authors/node()) and exists($date/node())"> · </xsl:if>
        <xsl:copy-of select="$date/node()"/>
      </div>
    </xsl:if>
    <h1 class="h3 mb-0"><xsl:apply-templates select="." mode="work-title"/></h1>
  </xsl:template>

  <xsl:template match="/request" mode="content">
    <xsl:variable name="description"><xsl:apply-templates select="." mode="work-description"/></xsl:variable>
    <xsl:variable name="metadata"><xsl:apply-templates select="." mode="work-metadata"/></xsl:variable>
    <xsl:variable name="sections"><xsl:apply-templates select="." mode="work-sections"/></xsl:variable>
    <xsl:variable name="files"><xsl:apply-templates select="." mode="work-files"/></xsl:variable>
    <xsl:if test="permissions/permission = 'edit'">
      <div class="d-flex justify-content-end gap-2 mb-3">
        <a class="btn btn-outline-secondary btn-sm" href="{context/@url}edit">Edit</a>
      </div>
    </xsl:if>
    <xsl:if test="exists($description/node())">
      <section id="publication" class="mb-4">
        <h2 class="h5">Publication</h2>
        <xsl:copy-of select="$description/node()"/>
      </section>
    </xsl:if>
    <xsl:if test="exists($metadata/node())">
      <section id="metadata" class="mb-4">
        <h2 class="h5">Additional Metadata</h2>
        <dl class="row mb-0"><xsl:copy-of select="$metadata/node()"/></dl>
      </section>
    </xsl:if>
    <xsl:copy-of select="$sections/node()"/>
    <xsl:if test="exists($files/node())">
      <section id="files" class="mb-4">
        <h2 class="h5">Files</h2>
        <xsl:copy-of select="$files/node()"/>
      </section>
    </xsl:if>
    <p class="small text-muted"><code><xsl:value-of select="context/@record_uri"/></code></p>
  </xsl:template>

</xsl:stylesheet>
