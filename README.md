# Home Lab Monitoring Stack

Monitoring stack for my Home Lab based on the [blog post](https://last9.io/blog/prometheus-with-docker-compose/) by Last9.

## Technologies

- Docker
- Prometheus
- Node Exporter
- cAdvisor
- Grafana
- Alertmanager

## Dashboards

- [Node Exporter Full](https://grafana.com/grafana/dashboards/1860-node-exporter-full/)
- [Docker Container & Host Metrics](https://grafana.com/grafana/dashboards/10619-docker-host-container-overview/)

## Setup

### Discord webhook for Alertmanager

Alertmanager reads the Discord webhook URL from a file at `/etc/alertmanager/discord_webhook` (mounted from `./alertmanager/discord_webhook`). The file is gitignored so the secret stays out of the repo.

On the host:

```sh
printf 'https://discord.com/api/webhooks/...' > alertmanager/discord_webhook
chmod 600 alertmanager/discord_webhook
docker compose up -d alertmanager
```

Verify the config loaded cleanly:

```sh
docker logs alertmanager 2>&1 | grep -iE "error|level=ERROR" | tail
```

If the file is missing or its content has no `https://` scheme, Alertmanager fails to load the config with `unsupported scheme "" for URL` and no notifications are delivered.
