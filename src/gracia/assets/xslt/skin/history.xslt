<?xml version="1.0" encoding="UTF-8"?>
<!-- The history of a record: a card for each version, newest first, in the style of the
     extensions: when it was made, and View (a dialog with the version in the editor, and what it
     changed since the one before) and Restore (it asks, and then saves the version as the new
     current one). -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="title">History of <xsl:value-of select="view/param[@name = 'heading']"/></xsl:template>

  <xsl:template match="/request" mode="header">
    <h1 class="h3 mb-0"><xsl:apply-templates select="." mode="title"/></h1>
    <div class="text-muted"><xsl:value-of select="view/param[@name = 'subtitle']"/></div>
  </xsl:template>

  <xsl:template match="/request" mode="content">
    <div class="d-flex justify-content-end gap-2 mb-3">
      <a class="btn btn-outline-secondary btn-sm" href="{view/param[@name = 'back']}">Back to the record</a>
    </div>
    <xsl:if test="empty(data/revisions/item)">
      <div class="text-muted">This record has no history yet.</div>
    </xsl:if>
    <xsl:for-each select="data/revisions/item">
      <div class="revision pb-3 mb-3 border-bottom" id="revision-{txn}">
        <h2 class="h5 mb-1">
          Version <xsl:value-of select="txn"/>
          <xsl:if test="current = 'true'"><span class="badge text-bg-success ms-1">current</span></xsl:if>
          <xsl:if test="deleted = 'true'"><span class="badge text-bg-danger ms-1">deleted</span></xsl:if>
        </h2>
        <p class="text-muted mb-2"><xsl:value-of select="when"/><xsl:text> </xsl:text><span class="small">(<xsl:value-of select="ago"/>)</span></p>
        <div class="d-flex flex-wrap align-items-center gap-2">
          <button type="button" class="btn btn-outline-secondary btn-sm"
                  data-history-view="{view}">View</button>
          <xsl:if test="can_restore = 'true'">
            <form method="post" action="{restore}" class="d-flex mb-0"
                  data-confirm="Restore version {txn}? It is saved as the new version of this record; the versions after it stay in the history.">
              <input type="hidden" name="txn" value="{txn}"/>
              <button type="submit" class="btn btn-outline-primary btn-sm">Restore</button>
            </form>
          </xsl:if>
        </div>
      </div>
    </xsl:for-each>

    <div class="modal fade" tabindex="-1" id="history-dialog">
      <div class="modal-dialog modal-xl">
        <div class="modal-content">
          <div class="modal-header">
            <h2 class="modal-title h5"><span data-title="">Version</span>
              <span class="text-muted small ms-2" data-when=""></span></h2>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body">
            <div class="alert alert-danger d-none" role="alert" data-problem=""></div>
            <ul class="nav nav-tabs" role="tablist">
              <li class="nav-item" role="presentation">
                <button type="button" class="nav-link active" data-bs-toggle="tab"
                        data-bs-target="#history-source" role="tab">Version</button>
              </li>
              <li class="nav-item" role="presentation">
                <button type="button" class="nav-link" data-bs-toggle="tab"
                        data-bs-target="#history-changes" role="tab">Changes since the version before</button>
              </li>
            </ul>
            <div class="tab-content pt-3">
              <div class="tab-pane active" id="history-source" role="tabpanel">
                <textarea class="form-control font-monospace" rows="22" readonly="readonly"
                          spellcheck="false" data-editor="turtle"></textarea>
              </div>
              <div class="tab-pane" id="history-changes" role="tabpanel">
                <pre class="border rounded p-2 bg-light small mb-0" data-diff=""></pre>
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
          </div>
        </div>
      </div>
    </div>
  </xsl:template>

</xsl:stylesheet>
