<?xml version="1.0" encoding="UTF-8"?>
<!-- The page of a work that follows Work-schemaorg-01.ttl: what the shape has of schema.org
     goes in the slots of work-base-01.xslt. The authors are an RDF list (`schema:author`),
     in the order they were given; a person that has a record is a link to it. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:f="https://gracia.pub/ns/functions" xmlns:work="https://gracia.pub/ns/work"
    xmlns:s="https://gracia.pub/ns/work/schemaorg" exclude-result-prefixes="#all">

  <xsl:import href="work-base-01.xslt"/>

  <!-- the values of a schema.org property of the resource, and of one of a blank node -->
  <xsl:function name="s:objects" as="element()*">
    <xsl:param name="name" as="xs:string"/>
    <xsl:sequence select="work:objects(concat('https://schema.org/', $name))"/>
  </xsl:function>

  <xsl:function name="s:of" as="element()*">
    <xsl:param name="node" as="element()"/>
    <xsl:param name="name" as="xs:string"/>
    <xsl:sequence select="f:objects($node, concat('https://schema.org/', $name))"/>
  </xsl:function>

  <xsl:template match="/request" mode="work-title">
    <xsl:variable name="name" select="s:objects('name')[1]"/>
    <xsl:value-of select="if (exists($name)) then f:text($name) else view/param[@name = 'heading']"/>
  </xsl:template>

  <xsl:template match="/request" mode="work-authors">
    <xsl:for-each select="f:list($work:resource, 'https://schema.org/author')">
      <xsl:variable name="link" select="f:link(., $work:repository)"/>
      <xsl:if test="position() gt 1">, </xsl:if>
      <xsl:choose>
        <xsl:when test="exists($link)"><a href="{$link}"><xsl:value-of select="f:name(.)"/></a></xsl:when>
        <xsl:otherwise><xsl:value-of select="f:name(.)"/></xsl:otherwise>
      </xsl:choose>
    </xsl:for-each>
  </xsl:template>

  <xsl:template match="/request" mode="work-date">
    <xsl:variable name="date" select="s:objects('datePublished')[1]"/>
    <xsl:if test="exists($date)"><time datetime="{f:text($date)}"><xsl:value-of select="f:text($date)"/></time></xsl:if>
  </xsl:template>

  <xsl:template match="/request" mode="work-description">
    <xsl:for-each select="s:objects('abstract')">
      <p><xsl:value-of select="f:text(.)"/></p>
    </xsl:for-each>
  </xsl:template>

  <xsl:template match="/request" mode="work-metadata">
    <xsl:variable name="pages" select="string-join((s:objects('pageStart')[1], s:objects('pageEnd')[1]) ! f:text(.), '–')"/>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Keywords'"/>
      <xsl:with-param name="values" select="s:objects('keywords')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Part of'"/>
      <xsl:with-param name="values" select="s:objects('isPartOf')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Publisher'"/>
      <xsl:with-param name="values" select="s:objects('publisher')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Volume'"/>
      <xsl:with-param name="values" select="s:objects('volumeNumber')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Issue'"/>
      <xsl:with-param name="values" select="s:objects('issueNumber')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Pages'"/>
      <xsl:with-param name="texts" select="$pages[. != '']"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'Language'"/>
      <xsl:with-param name="values" select="s:objects('inLanguage')"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'URL'"/>
      <xsl:with-param name="values" select="(s:objects('url'), s:objects('sameAs'))"/>
    </xsl:call-template>
    <xsl:call-template name="work:row">
      <xsl:with-param name="label" select="'License'"/>
      <xsl:with-param name="values" select="s:objects('license')"/>
    </xsl:call-template>
  </xsl:template>

  <xsl:template match="/request" mode="work-files">
    <xsl:variable name="files" select="f:children($work:resource, 'https://schema.org/encoding')"/>
    <xsl:if test="exists($files)">
      <ul class="list-group">
        <xsl:for-each select="$files">
          <xsl:variable name="url" select="s:of(., 'contentUrl')[1]"/>
          <xsl:variable name="name" select="(s:of(., 'name')[1], $url)[1]"/>
          <li class="list-group-item d-flex justify-content-between align-items-center">
            <xsl:choose>
              <xsl:when test="exists($url) and f:link($url, $work:repository)">
                <a href="{f:link($url, $work:repository)}"><xsl:value-of select="f:text($name)"/></a>
              </xsl:when>
              <xsl:otherwise><span><xsl:value-of select="f:text($name)"/></span></xsl:otherwise>
            </xsl:choose>
            <span class="small text-muted"><xsl:value-of
                select="string-join((s:of(., 'encodingFormat')[1], s:of(., 'contentSize')[1]) ! f:text(.), ', ')"/></span>
          </li>
        </xsl:for-each>
      </ul>
    </xsl:if>
  </xsl:template>

</xsl:stylesheet>
