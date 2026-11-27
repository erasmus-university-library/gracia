<?xml version="1.0" encoding="UTF-8"?>
<!-- The log of a job, with a choice of the log and Follow (static/scripts/job_log.js). -->
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" exclude-result-prefixes="#all">

  <xsl:template match="/request" mode="content">
    <xsl:variable name="name" select="view/param[@name = 'name']"/>
    <div data-job-log="" data-log-url="{view/param[@name = 'log_url']}"
         data-status-url="{view/param[@name = 'status_url']}">
      <div class="d-flex align-items-center gap-3 mb-2">
        <label class="form-label mb-0" for="log-name">Log</label>
        <select class="form-select form-select-sm w-auto" id="log-name" data-log-name="">
          <option value="luigi">
            <xsl:if test="$name = 'luigi'"><xsl:attribute name="selected">selected</xsl:attribute></xsl:if>
            luigi.log: what the job and its workers log</option>
          <option value="job">
            <xsl:if test="$name = 'job'"><xsl:attribute name="selected">selected</xsl:attribute></xsl:if>
            job.log: what the process printed</option>
        </select>
        <span class="badge text-bg-secondary" data-log-state=""></span>
        <div class="form-check ms-auto mb-0">
          <input class="form-check-input" type="checkbox" id="log-follow" data-log-follow="" checked="checked"/>
          <label class="form-check-label" for="log-follow">Follow</label>
        </div>
      </div>
      <pre class="bg-light border rounded p-2 small mb-0" style="height: 70vh; overflow: auto"
           data-log-text=""></pre>
    </div>
  </xsl:template>

</xsl:stylesheet>
