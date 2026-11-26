<?xml version="1.0" encoding="UTF-8"?>
<!-- The stylesheet of the debug view of a repository (`views/repository.py`): it is given
     the <request> (`?debug=input` shows it) and, for now, only says Hello, as text. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8"/>
  <xsl:template match="/request">
    <response>
      <headers>
        <Content-Type>text/plain</Content-Type>
      </headers>
      <body>Hello</body>
    </response>
  </xsl:template>
</xsl:stylesheet>
