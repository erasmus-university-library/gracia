<?xml version="1.0" encoding="UTF-8"?>
<!-- The page of a repository for a browser: a search box and what the search index finds. A first
     look, kept small on purpose (`RepositoryView.index` in `types/repository.py`). -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="content">
    <xsl:variable name="message" select="view/param[@name = 'message']"/>
    <xsl:variable name="total" select="xs:integer(view/param[@name = 'total'])"/>
    <form method="get" action="{view/param[@name = 'action']}" class="mb-4" role="search">
      <div class="input-group input-group-lg">
        <input type="search" name="q" value="{view/param[@name = 'query']}" class="form-control"
               id="search-q" placeholder="Search the records" autofocus="autofocus"
               aria-label="Search the records"/>
        <button type="submit" class="btn btn-primary">Search</button>
      </div>
    </form>
    <xsl:if test="$message != ''">
      <p class="text-muted" id="search-message"><xsl:value-of select="$message"/></p>
    </xsl:if>
    <xsl:if test="exists(data/hits/item)">
      <p class="text-muted small" id="search-count"><xsl:value-of
          select="view/param[@name = 'shown'], 'of', $total, if ($total = 1) then 'record' else 'records'"/> found.</p>
      <ul class="list-group" id="search-hits">
        <xsl:for-each select="data/hits/item">
          <li class="list-group-item d-flex justify-content-between align-items-center">
            <span>
              <xsl:choose>
                <xsl:when test="exists(url)"><a href="{url}"><xsl:value-of select="label"/></a></xsl:when>
                <xsl:otherwise><xsl:value-of select="label"/></xsl:otherwise>
              </xsl:choose>
              <xsl:if test="published = 'false'"><span class="badge text-bg-warning ms-1">not published</span></xsl:if>
            </span>
            <span class="text-muted small"><span class="badge text-bg-secondary me-2"><xsl:value-of select="type"/></span><code><xsl:value-of select="record"/></code></span>
          </li>
        </xsl:for-each>
      </ul>
    </xsl:if>
  </xsl:template>

</xsl:stylesheet>
