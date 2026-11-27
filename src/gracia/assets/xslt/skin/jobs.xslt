<?xml version="1.0" encoding="UTF-8"?>
<!-- The jobs of a repository: a list of the jobs that may be started, the job panel (a
     fragment, as it is made for the other pages that have one) and a dialog with the form
     of each job (fragments named form-<job>). -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="content">
    <div data-start-url="{view/param[@name = 'start_url']}">
      <h2 class="h5">Start a job</h2>
      <div class="list-group mb-4">
        <xsl:if test="empty(data/startable/item)">
          <div class="list-group-item text-muted">You may not start any job here.</div>
        </xsl:if>
        <xsl:for-each select="data/startable/item">
          <div class="list-group-item d-flex align-items-center gap-3">
            <div class="flex-grow-1">
              <div class="fw-semibold"><xsl:value-of select="title"/></div>
              <div class="text-muted small"><xsl:value-of select="description"/></div>
            </div>
            <button type="button" class="btn btn-primary" data-bs-toggle="modal"
                    data-bs-target="#start-{name}-modal">Start…</button>
          </div>
        </xsl:for-each>
      </div>

      <h2 class="h5">Jobs</h2>
      <xsl:copy-of select="fragments/fragment[@name = 'panel']/node()"/>
    </div>

    <!-- one dialog per job that may be started: its form, from the Colander schema of the job -->
    <xsl:for-each select="data/startable/item">
      <div class="modal fade" tabindex="-1" id="start-{name}-modal" data-job="{name}">
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header">
              <h2 class="modal-title h5"><xsl:value-of select="title"/></h2>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
              <xsl:if test="xs:integer(fields) = 0">
                <p class="text-muted">This job has no settings.</p>
              </xsl:if>
              <div class="alert alert-danger d-none job-problem" role="alert"></div>
              <div class="job-form">
                <xsl:copy-of select="/request/fragments/fragment[@name = concat('form-', current()/name)]/node()"/>
              </div>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
              <button type="submit" class="btn btn-primary" form="{form_id}">Start</button>
            </div>
          </div>
        </div>
      </div>
    </xsl:for-each>
  </xsl:template>

</xsl:stylesheet>
