<?xml version="1.0" encoding="UTF-8"?>
<!-- The browser of the API of a repository: a card for each operation, and a form to try the
     ones that only read (static/scripts/api.js). -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="content">
    <div id="api" data-base="{view/param[@name = 'base']}">
      <p class="small">
        <a href="{view/param[@name = 'document_url']}" data-json="">The OpenAPI document</a>
        (JSON, for the caller you are now).
      </p>
      <xsl:for-each select="data/operations/item">
        <div class="card mb-3" data-operation="">
          <div class="card-body">
            <div class="d-flex align-items-baseline gap-2 flex-wrap">
              <span class="badge {if (method = 'GET') then 'text-bg-success' else 'text-bg-warning'}"><xsl:value-of select="method"/></span>
              <code class="op-path"><xsl:value-of select="path"/></code>
              <span class="badge text-bg-light border ms-auto"><xsl:value-of select="permission"/></span>
            </div>
            <div class="mt-1"><xsl:value-of select="summary"/></div>
            <xsl:choose>
              <xsl:when test="method = 'GET'">
                <div class="mt-2 try" data-path="{path}" data-method="GET">
                  <form class="row g-2 align-items-end">
                    <div class="col-auto" data-inputs=""></div>
                    <div class="col-auto">
                      <button type="submit" class="btn btn-sm btn-outline-primary">Try it</button>
                    </div>
                  </form>
                  <div class="mt-2 small text-muted d-none" data-status=""></div>
                  <pre class="mt-2 mb-0 p-2 bg-light border rounded d-none small" data-body=""
                       style="max-height: 20rem; overflow: auto"></pre>
                </div>
              </xsl:when>
              <xsl:otherwise>
                <div class="mt-2 small text-muted">Not tried from here: it changes something.</div>
              </xsl:otherwise>
            </xsl:choose>
          </div>
        </div>
      </xsl:for-each>
      <xsl:if test="empty(data/operations/item)">
        <p class="text-muted">Nothing here is open to you.</p>
      </xsl:if>
    </div>
  </xsl:template>

</xsl:stylesheet>
