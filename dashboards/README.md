# Self-Hosted Dashboards

Self-hosted observability and analytics dashboards for BigQuery Agent Analytics (BQAA).

## Available Dashboards

| Dashboard                   | Primary Use Case                                                                            | Documentation                                               |
| :-------------------------- | :------------------------------------------------------------------------------------------ | :---------------------------------------------------------- |
| **Streamlit**               | Interactive Python-native telemetry inspection, latency distributions, token analytics      | [Streamlit Guide](streamlit/README.md)                      |
| **Grafana**                 | Time-series metrics, customizable SQL panels, public demo sharing, one-command local runner | [Grafana Guide](grafana/README.md)                          |
| **Looker Studio**           | Published 37-chart template over BQAA views, zero-install, team-wide reporting              | [Looker Studio Guide](looker_studio/README.md)              |
| **Power BI**                | Generated PBIP (TMDL + PBIR) port of the 37-chart Looker Studio dashboard; 1-click Web Configurator (`.zip` download + 8-page preview), Fabric REST, or Git deploy | [Power BI Guide](power_bi/README.md) · [Web Configurator](https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/power-bi/) |
| **Web (React/Vite)**        | Full-stack React + TypeScript dashboard with Vercel API backend (formerly `dashboard_v2/`)  | [Web App Guide](web/README.md)                              |
| **Demo Streamlit (Legacy)** | Original standalone demo app and Colab tutorial for raw ADK table logs                      | [Legacy Demo App](legacy_demo/README.md)                    |
