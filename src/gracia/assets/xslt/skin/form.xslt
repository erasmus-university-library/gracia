<?xml version="1.0" encoding="UTF-8"?>
<!-- The page of a form (templates/form.pt was): the form is a fragment, made by Deform; the
     heading has the class form-heading, and above the form are when the record was modified,
     the links to the source and the history, Saved and the errors. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="header">
    <h1 class="h3 mb-0 form-heading"><xsl:value-of select="view/param[@name = 'heading']"/></h1>
    <div class="text-muted"><xsl:value-of select="view/param[@name = 'subtitle']"/></div>
  </xsl:template>

  <xsl:template match="/request" mode="content">
    <xsl:variable name="modified" select="view/param[@name = 'modified']"/>
    <xsl:variable name="history" select="view/param[@name = 'history']"/>
    <xsl:variable name="toggle" select="data/toggle"/>
    <xsl:if test="$modified != ''">
      <div class="text-end text-muted small mb-1"><xsl:value-of select="$modified"/></div>
    </xsl:if>
    <xsl:if test="(exists($toggle) or $history != '') and site/@embed != 'true'">
      <div class="d-flex justify-content-end gap-2 mb-2">
        <xsl:if test="exists($toggle)">
          <a class="btn btn-outline-secondary btn-sm" href="{$toggle/url}"><xsl:value-of select="$toggle/label"/></a>
        </xsl:if>
        <xsl:if test="$history != ''">
          <a class="btn btn-outline-secondary btn-sm" href="{$history}">History</a>
        </xsl:if>
      </div>
    </xsl:if>
    <xsl:if test="view/param[@name = 'saved'] = 'true'">
      <div class="alert alert-success">Saved.</div>
    </xsl:if>
    <!-- a record that was saved in a dialog: embed.js tells the page around the dialog -->
    <xsl:for-each select="data/announce">
      <div id="gracia-saved" hidden="hidden" data-uri="{uri}" data-label="{label}"
           data-edit="{edit}" data-icon="{icon}" data-record="{record}"></div>
    </xsl:for-each>
    <xsl:for-each select="data/errors/item">
      <div class="alert alert-danger"><xsl:value-of select="."/></div>
    </xsl:for-each>
    <xsl:copy-of select="fragments/fragment[@name = 'form']/node()"/>
  </xsl:template>

</xsl:stylesheet>
