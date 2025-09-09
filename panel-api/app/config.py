from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str | None = None
    gcs_bucket: str | None = None
    internal_api_token: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

