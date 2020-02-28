FROM grafana/grafana

EXPOSE 3000

ENV GF_AUTH_ANONYMOUS_ENABLED "true"
ENV GF_AUTH_ANONYMOUS_ORG_ROLE "Editor"

COPY ./toil_dashboard.json /usr/share/grafana/public/dashboards/home.json
