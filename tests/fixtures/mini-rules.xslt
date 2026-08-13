<?xml version="1.0" encoding="UTF-8"?>
<!--
  A miniature Schematron-derived validator.

  Real rule sets are 200-900 KB and take ~1s to compile. This one emits the same
  SVRL vocabulary in a few lines, so engine tests run in milliseconds and fail for
  exactly one reason. It covers all three SVRL elements the parser cares about:
  fired-rule, failed-assert, and successful-report.
-->
<xsl:stylesheet version="2.0"
                xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                xmlns:svrl="http://purl.oclc.org/dsdl/svrl"
                xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">

  <xsl:output method="xml" indent="yes" encoding="UTF-8"/>

  <xsl:template match="/">
    <svrl:schematron-output title="mini test rules">

      <!-- Always evaluated, so there is always at least one fired rule. -->
      <svrl:fired-rule context="/Invoice"/>

      <!-- assert: fires when the test is FALSE -->
      <xsl:if test="not(/*/cbc:ID)">
        <svrl:failed-assert id="TEST-01" flag="fatal"
                            location="/Invoice[1]"
                            test="cbc:ID">
          <svrl:text>An Invoice shall have an Invoice number.</svrl:text>
        </svrl:failed-assert>
      </xsl:if>

      <!-- report: fires when the test is TRUE -->
      <xsl:if test="/*/cbc:Note">
        <svrl:fired-rule context="/Invoice/cbc:Note"/>
        <svrl:successful-report id="TEST-02" flag="warning"
                                location="/Invoice[1]/cbc:Note[1]"
                                test="cbc:Note">
          <svrl:text>A free-text note is present and will not be machine readable.</svrl:text>
        </svrl:successful-report>
      </xsl:if>

    </svrl:schematron-output>
  </xsl:template>

</xsl:stylesheet>
