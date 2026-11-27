<?xml version="1.0" encoding="UTF-8"?>
<!-- The page of the records of a type (views/type.py `type_page`): a page of them, the label
     of each with a link to it, and the pages under them. Page 1 is the newest records; the
     pages go back in time. It fills in the layout (layout.xslt), which comes before it.
     What it is given: the heading and the two totals as values of the view, the records of
     the page and the paging, and in the body the RDF/XML of those records (for their labels
     and slugs, by the helpers of rdf.xslt, which come before the layout). Addresses are
     made of the address of the context, which ends with a slash. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:f="https://gracia.pub/ns/functions" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="title">
    <xsl:value-of select="view/param[@name = 'heading']"/>
  </xsl:template>

  <xsl:template match="/request" mode="header">
    <xsl:variable name="published" select="xs:integer(view/param[@name = 'total_published'])"/>
    <xsl:variable name="all" select="xs:integer(view/param[@name = 'total_all'])"/>
    <h1 class="h3 mb-0"><xsl:value-of select="view/param[@name = 'heading']"/></h1>
    <div class="text-muted">
      <xsl:choose>
        <xsl:when test="permissions/permission = 'edit'">
          <xsl:value-of select="$published, 'published,', $all - $published, 'not published'"/>
        </xsl:when>
        <xsl:otherwise>
          <xsl:value-of select="$published, if ($published = 1) then 'record' else 'records'"/>
        </xsl:otherwise>
      </xsl:choose>
    </div>
  </xsl:template>

  <xsl:template match="/request" mode="content">
    <xsl:variable name="base" select="context/@url"/>
    <xsl:if test="permissions/permission = ('add', 'manage')">
      <div class="d-flex justify-content-end gap-2 mb-3">
        <xsl:if test="permissions/permission = 'add'">
          <a class="btn btn-outline-primary btn-sm" href="{$base}add">Add</a>
        </xsl:if>
        <xsl:if test="permissions/permission = 'manage'">
          <a class="btn btn-outline-secondary btn-sm" href="{$base}manage">Manage</a>
        </xsl:if>
      </div>
    </xsl:if>
    <xsl:choose>
      <xsl:when test="empty(records/record)">
        <p class="text-muted">There are no records here yet.</p>
      </xsl:when>
      <xsl:otherwise>
        <ul class="list-group mb-4">
          <xsl:for-each select="records/record">
            <xsl:variable name="node" select="f:record(@uri, .)"/>
            <xsl:variable name="number" select="f:number(@uri)"/>
            <xsl:variable name="slug" select="f:slug($node)"/>
            <xsl:variable name="label" select="f:label($node)"/>
            <li class="list-group-item d-flex justify-content-between align-items-center">
              <a href="{$base}{if ($slug != '') then $slug else $number}"><xsl:value-of
                  select="if ($label != '') then $label else '(Unnamed)'"/></a>
              <span class="small text-muted"><xsl:value-of select="$number"/></span>
            </li>
          </xsl:for-each>
        </ul>
      </xsl:otherwise>
    </xsl:choose>
    <xsl:apply-templates select="records" mode="pages"/>
  </xsl:template>

  <!-- the links to the pages: Newer, the first, the last and the ones near this one, with
       a gap where some are left out, and Older -->
  <xsl:template match="records[xs:integer(@pages) gt 1]" mode="pages">
    <xsl:variable name="base" select="/request/context/@url"/>
    <xsl:variable name="page" select="xs:integer(@page)"/>
    <xsl:variable name="pages" select="xs:integer(@pages)"/>
    <xsl:variable name="near" select="sort(distinct-values((1, $pages,
        max((1, $page - 2)) to min(($pages, $page + 2)))))"/>
    <nav aria-label="Pages">
      <ul class="pagination flex-wrap">
        <li class="page-item{if ($page le 1) then ' disabled' else ''}">
          <xsl:choose>
            <xsl:when test="$page gt 1">
              <a class="page-link" href="{$base}?page={$page - 1}">Newer</a>
            </xsl:when>
            <xsl:otherwise><span class="page-link">Newer</span></xsl:otherwise>
          </xsl:choose>
        </li>
        <xsl:for-each select="$near">
          <xsl:variable name="i" select="position()"/>
          <xsl:if test="$i gt 1 and . - $near[$i - 1] gt 1">
            <li class="page-item disabled"><span class="page-link">…</span></li>
          </xsl:if>
          <li class="page-item{if (. = $page) then ' active' else ''}">
            <a class="page-link" href="{$base}?page={.}"><xsl:value-of select="."/></a>
          </li>
        </xsl:for-each>
        <li class="page-item{if ($page ge $pages) then ' disabled' else ''}">
          <xsl:choose>
            <xsl:when test="$page lt $pages">
              <a class="page-link" href="{$base}?page={$page + 1}">Older</a>
            </xsl:when>
            <xsl:otherwise><span class="page-link">Older</span></xsl:otherwise>
          </xsl:choose>
        </li>
      </ul>
      <p class="small text-muted">Page <xsl:value-of select="$page"/> of <xsl:value-of
          select="$pages"/>. Page 1 has the newest records.</p>
    </nav>
  </xsl:template>

  <xsl:template match="records" mode="pages"/>

</xsl:stylesheet>
