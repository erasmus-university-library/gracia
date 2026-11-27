<?xml version="1.0" encoding="UTF-8"?>
<!-- The bare log of a job, for the iframe in the job panel: no layout, the log fills the frame and
     scrolls. The script is the same as on the log page, with its controls hidden. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    exclude-result-prefixes="#all">
  <xsl:output method="html" html-version="5" encoding="UTF-8" indent="yes" include-content-type="no"/>

  <xsl:template match="/request">
    <xsl:variable name="name" select="view/param[@name = 'name']"/>
    <response>
      <headers><Content-Type>text/html</Content-Type></headers>
      <body>
        <html lang="en">
          <head>
            <meta charset="utf-8"/>
            <title><xsl:value-of select="view/param[@name = 'heading']"/></title>
          </head>
          <body style="margin: 0">
            <div data-job-log="" data-log-url="{view/param[@name = 'log_url']}"
                 data-status-url="{view/param[@name = 'status_url']}">
              <select hidden="hidden" data-log-name=""><option value="{$name}" selected="selected"><xsl:value-of select="$name"/></option></select>
              <span hidden="hidden" data-log-state=""></span>
              <input type="checkbox" hidden="hidden" checked="checked" data-log-follow=""/>
              <pre style="margin: 0; padding: .4rem; height: 100vh; box-sizing: border-box; overflow: auto; font-size: .75rem; line-height: 1.25; background: #f8f9fa" data-log-text=""></pre>
            </div>
          </body>
        </html>
      </body>
    </response>
  </xsl:template>
</xsl:stylesheet>
