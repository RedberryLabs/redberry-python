"""Which Redberry API this build of the CLI talks to by default, and the known environments by address, so every
command says which one it uses. REDBERRY_API_URL and --api override the default."""

CHANNEL = "prod"

APIS = {"prod": "https://api.redberry.sbs", "dev": "https://api-dev.redberry.sbs"}
