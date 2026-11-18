<?xml version="1.0" encoding="UTF-8"?>
<!-- The doc of a person: the doc of every record, and the parts of the name (`gracia:docTemplate` of the type). -->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:map="http://www.w3.org/2005/xpath-functions/map"
    xmlns:doc="urn:gracia:doc"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:foaf="http://xmlns.com/foaf/0.1/"
    exclude-result-prefixes="#all">

  <xsl:import href="type:type/doc/doc-base-01.xsl"/>

  <!-- the fields it adds, for the schema mode (see the base) -->
  <xsl:template match="/types/type[@slug = 'person']" mode="schema" as="map(*)*">
    <xsl:variable name="base" as="map(*)*"><xsl:next-match/></xsl:variable>
    <xsl:sequence select="($base, doc:field('given_name', 'text'), doc:field('family_name', 'text'))"/>
  </xsl:template>

  <xsl:template match="/rdf:RDF/*[starts-with(@rdf:about, 'rec:person:')]" mode="doc" as="map(*)">
    <xsl:variable name="base" as="map(*)"><xsl:next-match/></xsl:variable>
    <xsl:variable name="given" select="doc:values(., xs:QName('foaf:givenName'))"/>
    <xsl:variable name="family" select="doc:values(., xs:QName('foaf:familyName'))"/>
    <xsl:sequence select="map:merge(($base, doc:fields(map {
        'given_name':  $given[1],
        'family_name': $family[1]
    })))"/>
  </xsl:template>
</xsl:stylesheet>
