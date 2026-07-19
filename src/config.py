from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # GitHub App
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_private_key_path: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    github_webhook_secret: str = ""

    # Slack App
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    slack_bot_token: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://pulley:pulley@localhost:5432/pulley"

    # App
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Comma-separated `owner/repo` list. Empty = act on every installed repo.
    github_allowed_repos: str = ""
    # Comma-separated `owner/repo` list never scanned, even if otherwise allowed.
    github_excluded_repos: str = ""

    # In-app scheduler. Off by default so serverless/scale-to-zero deployments
    # aren't forced always-on; enable only on a single always-on instance.
    scheduler_enabled: bool = False
    # Fallback recap schedule when an org's recap_cron column is NULL. 5-field, UTC.
    recap_cron: str = "0 9 * * 1-5"
    # Stale-reminder schedule for every org. 5-field, UTC.
    stale_reminder_cron: str = "0 14 * * 1-5"
    # When empty, /internal/* endpoints return 404; when set they require
    # Authorization: Bearer <token>.
    internal_api_token: str = ""

    @property
    def base_url(self) -> str:
        return self.app_base_url

    @property
    def allowed_repos(self) -> set[str]:
        return {r.strip() for r in self.github_allowed_repos.split(",") if r.strip()}

    @property
    def excluded_repos(self) -> set[str]:
        return {r.strip() for r in self.github_excluded_repos.split(",") if r.strip()}

    @property
    def github_private_key_pem(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            with open(self.github_app_private_key_path) as f:
                return f.read()
        raise ValueError("No GitHub App private key configured")


settings = Settings()
