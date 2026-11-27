<?xml version="1.0" encoding="UTF-8"?>
<!-- How to jump between the resources of the RDF/XML that a page is given in the body of the
     request, with keys, so that it is a lookup and not a search. views/xslt.py `page` puts
     it before the layout and the page, so every skin has it, a repository's own too.

     The RDF/XML is flat (what pyoxigraph writes): each resource is an element child of
     rdf:RDF with an rdf:about (a record) or an rdf:nodeID (a blank node), and its
     properties are the elements in it, with the path of a property its namespace and its
     local name together. An object that is a resource is an rdf:resource, a literal is the
     text; a blank node is an rdf:nodeID, which the description of the node has.

       f:record($uri, $context)   the description of a record of the uri
       f:resource($record)        the description of its resource (the blank node)
       f:objects($node, $path)    the elements of a property of a node (a literal or a uri)
       f:label($record), f:slug($record)   what the record has of them, or the empty string
       f:number($uri)             the number of a record uri: rec:person:7 is 7
       f:children($node, $path)   the descriptions of the blank nodes that a property of a node has
       f:list($node, $path)       the items of the list (rdf:first) that a property of a node has
       f:name($property)          the label of the record that a value refers to, else its text
       f:text($property)          what to show of a value
       f:link($property, $repository)   where a value goes to, if anywhere -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:g="https://gracia.pub/ns/" xmlns:f="https://gracia.pub/ns/functions"
    exclude-result-prefixes="#all">

  <xsl:key name="f:about" match="/request/body/rdf:RDF/*" use="@rdf:about"/>
  <xsl:key name="f:node" match="/request/body/rdf:RDF/*" use="@rdf:nodeID"/>
  <!-- the records, by the resource uri that they have: what a reference to one says -->
  <xsl:key name="f:resource-uri" match="/request/body/rdf:RDF/*[g:resource_uri]"
           use="g:resource_uri/@rdf:resource"/>
  <!-- the properties, by the resource they are of and their path -->
  <xsl:key name="f:property" match="/request/body/rdf:RDF/*/*"
           use="concat((../@rdf:about, ../@rdf:nodeID)[1], ' ', namespace-uri(), local-name())"/>

  <!-- a web address, a mail address or a phone number is a link; any other is only text -->
  <xsl:variable name="f:schemes" select="('http', 'https', 'mailto', 'tel')"/>

  <xsl:function name="f:record" as="element()?">
    <xsl:param name="uri" as="xs:string"/>
    <xsl:param name="context" as="node()"/>
    <xsl:sequence select="key('f:about', $uri, root($context))[1]"/>
  </xsl:function>

  <xsl:function name="f:resource" as="element()?">
    <xsl:param name="record" as="element()?"/>
    <xsl:sequence select="key('f:node', $record/g:resource/@rdf:nodeID, root($record))[1]"/>
  </xsl:function>

  <xsl:function name="f:objects" as="element()*">
    <xsl:param name="node" as="element()?"/>
    <xsl:param name="path" as="xs:string"/>
    <xsl:sequence select="key('f:property',
        concat(($node/@rdf:about, $node/@rdf:nodeID)[1], ' ', $path), root($node))
        [exists(@rdf:resource) or not(@rdf:nodeID)]"/>
  </xsl:function>

  <xsl:function name="f:children" as="element()*">
    <xsl:param name="node" as="element()?"/>
    <xsl:param name="path" as="xs:string"/>
    <xsl:sequence select="for $property in key('f:property',
        concat(($node/@rdf:about, $node/@rdf:nodeID)[1], ' ', $path), root($node))
        return key('f:node', string($property/@rdf:nodeID), root($node))[1]"/>
  </xsl:function>

  <xsl:function name="f:list" as="element()*">
    <xsl:param name="node" as="element()?"/>
    <xsl:param name="path" as="xs:string"/>
    <xsl:variable name="first" select="key('f:property',
        concat(($node/@rdf:about, $node/@rdf:nodeID)[1], ' ', $path), root($node))
        [exists(@rdf:nodeID)][1]"/>
    <xsl:sequence select="f:list-items(key('f:node', string($first/@rdf:nodeID), root($node))[1])"/>
  </xsl:function>

  <xsl:function name="f:list-items" as="element()*">
    <xsl:param name="cell" as="element()?"/>
    <xsl:if test="exists($cell)">
      <xsl:sequence select="$cell/rdf:first"/>
      <xsl:sequence select="f:list-items(key('f:node', string($cell/rdf:rest/@rdf:nodeID), root($cell))[1])"/>
    </xsl:if>
  </xsl:function>

  <xsl:function name="f:name" as="xs:string">
    <xsl:param name="property" as="element()"/>
    <xsl:variable name="record" select="key('f:resource-uri', string($property/@rdf:resource), root($property))[1]"/>
    <xsl:variable name="label" select="f:label($record)"/>
    <xsl:sequence select="if ($label != '') then $label else f:text($property)"/>
  </xsl:function>

  <xsl:function name="f:label" as="xs:string">
    <xsl:param name="record" as="element()?"/>
    <xsl:sequence select="string($record/g:label[1])"/>
  </xsl:function>

  <xsl:function name="f:slug" as="xs:string">
    <xsl:param name="record" as="element()?"/>
    <xsl:sequence select="string($record/g:slug[1])"/>
  </xsl:function>

  <xsl:function name="f:number" as="xs:string">
    <xsl:param name="uri" as="xs:string"/>
    <xsl:sequence select="tokenize($uri, ':')[last()]"/>
  </xsl:function>

  <xsl:function name="f:text" as="xs:string">
    <xsl:param name="property" as="element()"/>
    <xsl:sequence select="string(($property/@rdf:resource, $property)[1])"/>
  </xsl:function>

  <xsl:function name="f:link" as="xs:string?">
    <xsl:param name="property" as="element()"/>
    <xsl:param name="repository" as="xs:string"/>
    <xsl:variable name="address" select="string($property/@rdf:resource)"/>
    <xsl:variable name="record" select="key('f:resource-uri', $address, root($property))[1]"/>
    <xsl:choose>
      <xsl:when test="$address = ''"/>
      <xsl:when test="exists($record)">
        <xsl:variable name="parts" select="tokenize($record/@rdf:about, ':')"/>
        <xsl:sequence select="concat($repository, $parts[2], '/', $parts[3])"/>
      </xsl:when>
      <xsl:when test="lower-case(substring-before($address, ':')) = $f:schemes">
        <xsl:sequence select="$address"/>
      </xsl:when>
    </xsl:choose>
  </xsl:function>

</xsl:stylesheet>
