<?xml version="1.0" encoding="UTF-8"?>
<!-- The page of a record (types/type.py `RecordView.index`): its label, its type, the description
     when the shape of the type has one, and a table of its key information, each property
     under the name the shape gives it. It fills in the layout (layout.xslt), which comes
     before it. What it is given: the heading and the name of the type as values of the view,
     the properties of the shape that may be shown (their paths, names and roles), and in the
     body the RDF/XML of the record and of what it is made of: a value that is the resource
     uri of another record of it is a link to that record. The helpers of rdf.xslt, which
     come before the layout, jump from the record to its resource and to its properties. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:f="https://gracia.pub/ns/functions" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="title">
    <xsl:value-of select="view/param[@name = 'heading']"/>
  </xsl:template>

  <xsl:template match="/request" mode="header">
    <h1 class="h3 mb-0"><xsl:value-of select="view/param[@name = 'heading']"/></h1>
    <div class="text-muted">
      <a href="{context/node[last() - 1]/@url}"><xsl:value-of
          select="view/param[@name = 'type_name']"/></a>
    </div>
  </xsl:template>

  <xsl:template match="/request" mode="content">
    <xsl:variable name="request" select="."/>
    <xsl:variable name="resource" select="f:resource(f:record(context/@record_uri, .))"/>
    <xsl:variable name="repository" select="string(context/node[@kind = 'Repository']/@url)"/>
    <xsl:if test="permissions/permission = 'edit'">
      <div class="d-flex justify-content-end gap-2 mb-3">
        <a class="btn btn-outline-secondary btn-sm" href="{context/@url}edit">Edit</a>
      </div>
    </xsl:if>
    <!-- the description: the first property with that role that the record has -->
    <xsl:variable name="description" select="(for $path in properties/property[@role = 'description']/@path
        return if (exists(f:objects($resource, $path))) then $path else ())[1]"/>
    <xsl:if test="exists($description)">
      <xsl:for-each select="sort(f:objects($resource, $description), (), f:text#1)">
        <p class="lead"><xsl:value-of select="f:text(.)"/></p>
      </xsl:for-each>
    </xsl:if>
    <xsl:variable name="rows" select="for $path in distinct-values(properties/property[@role = 'key_info']/@path)
        return if (exists(f:objects($resource, $path))) then $path else ()"/>
    <xsl:if test="exists($rows)">
      <table class="table">
        <tbody>
          <xsl:for-each select="$rows">
            <xsl:variable name="path" select="."/>
            <tr>
              <th scope="row" class="w-25"><xsl:value-of
                  select="$request/properties/property[@path = $path][1]/@name"/></th>
              <td>
                <xsl:for-each select="sort(f:objects($resource, $path), (), f:text#1)">
                  <xsl:variable name="link" select="f:link(., $repository)"/>
                  <div>
                    <xsl:choose>
                      <xsl:when test="exists($link)">
                        <a href="{$link}"><xsl:value-of select="f:text(.)"/></a>
                      </xsl:when>
                      <xsl:otherwise><span><xsl:value-of select="f:text(.)"/></span></xsl:otherwise>
                    </xsl:choose>
                  </div>
                </xsl:for-each>
              </td>
            </tr>
          </xsl:for-each>
        </tbody>
      </table>
    </xsl:if>
    <p class="small text-muted"><code><xsl:value-of select="context/@record_uri"/></code></p>
  </xsl:template>

</xsl:stylesheet>
