<?xml version="1.0" encoding="UTF-8"?>
<!--
  Nested RDF/XML (as written by rdfxml-to-nested.xsl) -> a simple HTML page.

  One section per top-level resource, with a table of its properties:
    - the node element name is the type (rdf:Description = no type)
    - rdf:resource            -> link
    - rdf:nodeID              -> link to the shared blank node further down
    - nested node element     -> nested table (blank nodes written inline)
    - rdf:parseType="Collection" -> numbered list
    - literal                 -> text, with its language or datatype as a small note
  Property and type names are the prefixed names of the source, so the prefixes of the
  gracia prefix table show up here too.

  Run:  gracia export -f rdfxml-collection | gracia transform rdfxml-to-html.xsl
        gracia export -f html-table                  (same, built in)
-->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    exclude-result-prefixes="#all">

  <xsl:output method="html" html-version="5" indent="yes" encoding="UTF-8"/>

  <xsl:param name="title" as="xs:string" select="'RDF resources'"/>

  <xsl:template match="/rdf:RDF">
    <html lang="en">
      <head>
        <meta charset="utf-8"/>
        <title><xsl:value-of select="$title"/></title>
        <style>
          body { font-family: sans-serif; margin: 2em; color: #222; }
          h2 { font-size: 1.1em; margin: 1.5em 0 .3em; word-break: break-all; }
          table { border-collapse: collapse; margin: .2em 0; }
          th, td { border: 1px solid #ccc; padding: .25em .6em; vertical-align: top; text-align: left; }
          th { background: #f4f4f4; font-weight: normal; white-space: nowrap; }
          td table { margin: 0; }
          ol { margin: 0; padding-left: 1.4em; }
          .meta { color: #888; font-size: .8em; margin-left: .4em; }
          a { color: #0645ad; }
        </style>
      </head>
      <body>
        <h1><xsl:value-of select="$title"/></h1>
        <p><xsl:value-of select="count(*)"/> resource(s)</p>
        <xsl:apply-templates select="*" mode="resource"/>
      </body>
    </html>
  </xsl:template>

  <!-- a top-level node element: heading + property table -->
  <xsl:template match="rdf:RDF/*" mode="resource">
    <section>
      <h2>
        <xsl:choose>
          <xsl:when test="@rdf:about">
            <a href="{@rdf:about}"><xsl:value-of select="@rdf:about"/></a>
          </xsl:when>
          <xsl:otherwise>
            <xsl:attribute name="id" select="'b-' || @rdf:nodeID"/>
            <xsl:text>_:</xsl:text><xsl:value-of select="@rdf:nodeID"/>
          </xsl:otherwise>
        </xsl:choose>
      </h2>
      <xsl:call-template name="properties"/>
    </section>
  </xsl:template>

  <!-- a nested node element (an inline blank node, or a resource described in place) -->
  <xsl:template match="*" mode="node">
    <xsl:call-template name="properties"/>
  </xsl:template>

  <!-- the context item is a node element -->
  <xsl:template name="properties">
    <table>
      <xsl:if test="not(self::rdf:Description)">
        <tr><th>type</th><td><xsl:value-of select="name()"/></td></tr>
      </xsl:if>
      <xsl:for-each select="*">
        <tr>
          <th><xsl:value-of select="name()"/></th>
          <td><xsl:apply-templates select="." mode="value"/></td>
        </tr>
      </xsl:for-each>
    </table>
  </xsl:template>

  <!-- the context item is a property element -->
  <xsl:template match="*" mode="value">
    <xsl:choose>
      <xsl:when test="@rdf:resource">
        <a href="{@rdf:resource}"><xsl:value-of select="@rdf:resource"/></a>
      </xsl:when>
      <xsl:when test="@rdf:nodeID">
        <a href="#b-{@rdf:nodeID}">_:<xsl:value-of select="@rdf:nodeID"/></a>
      </xsl:when>
      <xsl:when test="@rdf:parseType = 'Collection'">
        <ol>
          <xsl:for-each select="*">
            <li><xsl:apply-templates select="." mode="item"/></li>
          </xsl:for-each>
        </ol>
      </xsl:when>
      <xsl:when test="*">
        <xsl:apply-templates select="*" mode="node"/>
      </xsl:when>
      <xsl:otherwise>
        <xsl:value-of select="."/>
        <xsl:if test="@xml:lang">
          <span class="meta">@<xsl:value-of select="@xml:lang"/></span>
        </xsl:if>
        <xsl:if test="@rdf:datatype">
          <span class="meta">^^<xsl:value-of select="tokenize(@rdf:datatype, '[#/]')[last()]"/></span>
        </xsl:if>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <!-- a member of a collection: a node element, usually only a reference -->
  <xsl:template match="*" mode="item">
    <xsl:choose>
      <xsl:when test="not(*) and @rdf:about">
        <a href="{@rdf:about}"><xsl:value-of select="@rdf:about"/></a>
      </xsl:when>
      <xsl:when test="not(*) and @rdf:nodeID">
        <a href="#b-{@rdf:nodeID}">_:<xsl:value-of select="@rdf:nodeID"/></a>
      </xsl:when>
      <xsl:otherwise>
        <xsl:call-template name="properties"/>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>

</xsl:stylesheet>
