<?xml version="1.0" encoding="UTF-8"?>
<!-- The page around the pages of the site, as templates/layout.pt was. It is the first of two
     stylesheets that views/xslt.py `page` puts together: the stylesheet of the page comes
     after it and fills in what the page has of its own, by templates for the request in the
     modes "title", "header", "content" and "footer", as a template fills the slots of the
     layout. Here are what a page has when it does not say: the title is the heading, the
     header is the title, the footer says gracia. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <!-- Saxon writes all of the response, and the page is in it as elements: the writer is the
       one of HTML5 (no doctype, the root is the response: the renderer adds it), and the
       renderer cuts the headers and the body out of the text (views/xslt.py parse_response) -->
  <xsl:output method="html" html-version="5" encoding="UTF-8" indent="yes"
              include-content-type="no"/>

  <xsl:template match="/request">
    <xsl:variable name="embed" select="site/@embed = 'true'"/>
    <response>
      <headers>
        <Content-Type>text/html</Content-Type>
      </headers>
      <body>
        <html lang="en">
          <head>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1"/>
            <title><xsl:apply-templates select="." mode="title"/></title>
            <link rel="icon" type="image/svg+xml" href="{site/@favicon}"/>
          </head>
          <body>
            <xsl:if test="not($embed)">
              <header class="container py-3 border-bottom mb-4">
                <a class="gracia-logo mb-3" href="{site/@home}" aria-label="gracia, home">
                  <span class="wave" aria-hidden="true">👋</span><span>gracia</span>
                </a>
                <xsl:apply-templates select="." mode="header"/>
              </header>
            </xsl:if>
            <main class="{if ($embed) then 'container-fluid px-4 py-4' else 'container pb-4'}">
              <xsl:for-each select="site/flash">
                <div class="alert alert-{@level} alert-dismissible fade show" role="alert">
                  <xsl:value-of select="."/>
                  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                </div>
              </xsl:for-each>
              <xsl:apply-templates select="." mode="content"/>
            </main>
            <xsl:if test="not($embed)">
              <footer class="container py-3 border-top text-muted small">
                <xsl:apply-templates select="." mode="footer"/>
              </footer>
            </xsl:if>
          </body>
        </html>
      </body>
    </response>
  </xsl:template>

  <xsl:template match="/request" mode="title">
    <xsl:value-of select="(view/param[@name = 'heading'], 'gracia')[1]"/>
  </xsl:template>

  <xsl:template match="/request" mode="header">
    <h1 class="h3 mb-0"><xsl:apply-templates select="." mode="title"/></h1>
    <xsl:if test="view/param[@name = 'subtitle'] != ''">
      <div class="text-muted"><xsl:value-of select="view/param[@name = 'subtitle']"/></div>
    </xsl:if>
  </xsl:template>

  <xsl:template match="/request" mode="content"/>

  <xsl:template match="/request" mode="footer">gracia</xsl:template>

</xsl:stylesheet>
