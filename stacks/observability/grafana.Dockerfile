FROM grafana/grafana:13.3.0@sha256:488984b716c0862639f91a698adbe2862677a5a07bad42da5f03f3030883b205

# VictoriaLogs' data source, signed by its maker and checked against its release's checksum, baked in so Grafana
# fetches nothing at start. Outside /var/lib/grafana, which is a volume.
ADD --checksum=sha256:72c7dbe4bc046f278670f6ab4c4b1518201037f35f534eff462f76fa6ef771be \
    https://github.com/VictoriaMetrics/victorialogs-datasource/releases/download/v0.34.0/victoriametrics-logs-datasource-v0.34.0.tar.gz \
    /tmp/victorialogs.tar.gz
# Grafana's own Metrics Drilldown app, the sidebar's Drilldown, checked against the checksum grafana.com lists for
# the version. Each version names the Grafana versions it runs on, so it moves with the image above.
ADD --checksum=sha256:06429398b25f087af6f951801f823b86eae280dd168f510a542b092d8bad1451 \
    https://grafana.com/api/plugins/grafana-metricsdrilldown-app/versions/3.1.0/download \
    /tmp/metricsdrilldown.zip
USER root
RUN mkdir -p /usr/share/grafana/plugins-homelab \
    && tar -xzf /tmp/victorialogs.tar.gz -C /usr/share/grafana/plugins-homelab \
    && unzip -q /tmp/metricsdrilldown.zip -d /usr/share/grafana/plugins-homelab \
    && rm /tmp/victorialogs.tar.gz /tmp/metricsdrilldown.zip
USER 472
ENV GF_PATHS_PLUGINS=/usr/share/grafana/plugins-homelab
