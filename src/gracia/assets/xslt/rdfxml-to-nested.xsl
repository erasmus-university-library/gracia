<?xml version="1.0" encoding="UTF-8"?>
<!--
  Flat RDF/XML (as pyoxigraph writes it) -> nested RDF/XML, with rdf:parseType="Collection".

  Source: the RDF/XML of `gracia`'s Dataset serializer: every subject spread over one or more
  rdf:Description / typed node elements, with rdf:about, rdf:nodeID, rdf:resource, literals.
  Output: one element per resource
    - a blank node that is referenced exactly once is nested where it is used
    - a blank node that is shared (or not referenced at all) is written at top level and
      referenced through rdf:nodeID
    - an rdf:first/rdf:rest chain is written as rdf:parseType="Collection" when that can
      express it. RDF/XML collections only hold node elements, so a list with a literal item,
      or with extra triples on a cell, stays an explicit rdf:first/rdf:rest chain.
  Element names and namespace declarations of the source are reused, so the prefixes of the
  gracia prefix table carry through.

  Not a general RDF/XML reader: it understands the flat shape above (no rdf:parseType,
  rdf:ID, rdf:li, or properties that already nest their objects).

  Your own stylesheets (`gracia export -x`) get this nested RDF/XML as their source document.

  Parameters:
    record_uri   the uri of the resource the document is about (the index view of a record gives
                 it): its element is the first one. Empty: the order of the source.
-->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:g="urn:gracia:xslt"
    exclude-result-prefixes="#all">

  <xsl:output method="xml" indent="yes" encoding="UTF-8"/>
  <xsl:param name="record_uri" as="xs:string" select="''"/>
  <xsl:variable name="root" as="document-node()" select="/"/>
  <xsl:variable name="rdf-ns" as="xs:string" select="'http://www.w3.org/1999/02/22-rdf-syntax-ns#'"/>

  <xsl:function name="g:subject" as="xs:string">
    <xsl:param name="node" as="element()"/>
    <xsl:sequence select="if ($node/@rdf:about) then string($node/@rdf:about) else '_:' || $node/@rdf:nodeID"/>
  </xsl:function>

  <!-- a subject can be spread over several node elements: index them by subject -->
  <xsl:key name="nodes" match="/rdf:RDF/*" use="g:subject(.)"/>
  <xsl:key name="props" match="/rdf:RDF/*/*" use="g:subject(..)"/>
  <xsl:key name="refs"  match="/rdf:RDF/*/*[@rdf:nodeID]" use="'_:' || @rdf:nodeID"/>

  <xsl:function name="g:refs" as="xs:integer">
    <xsl:param name="bnode" as="xs:string"/>
    <xsl:sequence select="count(key('refs', $bnode, $root))"/>
  </xsl:function>

  <xsl:function name="g:is-list" as="xs:boolean">
    <xsl:param name="cell" as="xs:string"/>
    <xsl:variable name="ps" select="key('props', $cell, $root)"/>
    <xsl:variable name="first" select="$ps[self::rdf:first]"/>
    <xsl:variable name="rest" select="$ps[self::rdf:rest]"/>
    <xsl:sequence select="
        g:refs($cell) = 1 and count($ps) = 2 and count($first) = 1 and count($rest) = 1
        and empty(key('nodes', $cell, $root)[not(self::rdf:Description)])
        and exists($first/(@rdf:resource, @rdf:nodeID))
        and ($rest/@rdf:resource = $rdf-ns || 'nil'
             or ($rest/@rdf:nodeID and g:is-list('_:' || $rest/@rdf:nodeID)))"/>
  </xsl:function>

  <xsl:function name="g:list-items" as="element()*">
    <xsl:param name="cell" as="xs:string"/>
    <xsl:variable name="ps" select="key('props', $cell, $root)"/>
    <xsl:sequence select="$ps[self::rdf:first]"/>
    <xsl:if test="$ps[self::rdf:rest]/@rdf:nodeID">
      <xsl:sequence select="g:list-items('_:' || $ps[self::rdf:rest]/@rdf:nodeID)"/>
    </xsl:if>
  </xsl:function>

  <xsl:template match="/rdf:RDF">
    <xsl:copy>
      <xsl:for-each-group select="*[@rdf:about]" group-by="@rdf:about">
        <xsl:sort select="if (current-grouping-key() = $record_uri) then 0 else 1" data-type="number"/>
        <xsl:call-template name="node">
          <xsl:with-param name="id" select="current-grouping-key()"/>
          <xsl:with-param name="top" select="true()"/>
        </xsl:call-template>
      </xsl:for-each-group>
      <xsl:for-each-group select="*[@rdf:nodeID][g:refs('_:' || @rdf:nodeID) != 1]" group-by="@rdf:nodeID">
        <xsl:call-template name="node">
          <xsl:with-param name="id" select="'_:' || current-grouping-key()"/>
          <xsl:with-param name="top" select="true()"/>
        </xsl:call-template>
      </xsl:for-each-group>
    </xsl:copy>
  </xsl:template>

  <xsl:template name="node">
    <xsl:param name="id" as="xs:string"/>
    <xsl:param name="top" as="xs:boolean" select="false()"/>
    <xsl:variable name="typed" select="(key('nodes', $id, $root)[not(self::rdf:Description)])[1]"/>
    <xsl:element name="{if ($typed) then name($typed) else 'rdf:Description'}"
                 namespace="{if ($typed) then namespace-uri($typed) else $rdf-ns}">
      <xsl:choose>
        <xsl:when test="starts-with($id, '_:')">
          <xsl:if test="$top"><xsl:attribute name="rdf:nodeID" select="substring($id, 3)"/></xsl:if>
        </xsl:when>
        <xsl:otherwise><xsl:attribute name="rdf:about" select="$id"/></xsl:otherwise>
      </xsl:choose>
      <xsl:apply-templates select="key('props', $id, $root)" mode="prop"/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="*" mode="prop">
    <xsl:element name="{name()}" namespace="{namespace-uri()}">
      <xsl:choose>
        <xsl:when test="@rdf:nodeID">
          <xsl:variable name="b" select="'_:' || @rdf:nodeID"/>
          <xsl:choose>
            <xsl:when test="not(self::rdf:rest) and g:refs($b) = 1 and g:is-list($b)">
              <xsl:attribute name="rdf:parseType">Collection</xsl:attribute>
              <xsl:for-each select="g:list-items($b)">
                <xsl:choose>
                  <xsl:when test="@rdf:resource"><rdf:Description rdf:about="{@rdf:resource}"/></xsl:when>
                  <xsl:when test="g:refs('_:' || @rdf:nodeID) = 1">
                    <xsl:call-template name="node"><xsl:with-param name="id" select="'_:' || @rdf:nodeID"/></xsl:call-template>
                  </xsl:when>
                  <xsl:otherwise><rdf:Description rdf:nodeID="{@rdf:nodeID}"/></xsl:otherwise>
                </xsl:choose>
              </xsl:for-each>
            </xsl:when>
            <xsl:when test="g:refs($b) = 1">
              <xsl:call-template name="node"><xsl:with-param name="id" select="$b"/></xsl:call-template>
            </xsl:when>
            <xsl:otherwise><xsl:copy-of select="@rdf:nodeID"/></xsl:otherwise>
          </xsl:choose>
        </xsl:when>
        <xsl:otherwise>
          <xsl:copy-of select="@*"/>
          <xsl:value-of select="."/>
        </xsl:otherwise>
      </xsl:choose>
    </xsl:element>
  </xsl:template>
</xsl:stylesheet>
