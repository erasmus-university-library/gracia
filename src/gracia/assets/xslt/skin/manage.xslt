<?xml version="1.0" encoding="UTF-8"?>
<!-- The manage pages of a repository and of a type (templates/manage.pt was): a strip of tabs
     and a pane for each, the HTML of which is a fragment named pane-<name>. -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="content">
    <!-- a tab is a named form (gracia.views.tabs); its pane is what the tab renders -->
    <ul class="nav nav-tabs" role="tablist">
      <xsl:for-each select="data/panes/item">
        <li class="nav-item" role="presentation">
          <button type="button" class="nav-link{if (active = 'true') then ' active' else ''}"
                  data-bs-toggle="tab" data-bs-target="#tab-{name}" data-tab="{name}"
                  role="tab" aria-selected="{active}"><xsl:value-of select="title"/></button>
        </li>
      </xsl:for-each>
    </ul>
    <div class="tab-content pt-3">
      <xsl:for-each select="data/panes/item">
        <div class="tab-pane{if (active = 'true') then ' active' else ''}" id="tab-{name}"
             role="tabpanel">
          <xsl:copy-of select="/request/fragments/fragment[@name = concat('pane-', current()/name)]/node()"/>
        </div>
      </xsl:for-each>
    </div>
  </xsl:template>

</xsl:stylesheet>
