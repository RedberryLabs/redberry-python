"""What ``setup-azure`` and ``setup-aws`` share: their error, and the Vercel OIDC token's issuer, audience
and subject that a federated credential or an IAM role trust policy names."""


class SetupError(Exception):
    pass


def oidc_issuer(team: str) -> str:
    return f"https://oidc.vercel.com/{team}"


def oidc_audience(team: str) -> str:
    return f"https://vercel.com/{team}"


def oidc_subject(team: str, project: str, environment: str = "production") -> str:
    return f"owner:{team}:project:{project}:environment:{environment}"
