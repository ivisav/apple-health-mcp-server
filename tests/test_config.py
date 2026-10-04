from pathlib import Path

from app.config import Settings


def test_unknown_keys_in_env_file_are_ignored(tmp_path: Path) -> None:
    # Settings removed from the code (e.g. old ES_* backend keys) may still sit in a
    # user's config/.env; they must not stop the server from starting.
    env = tmp_path / ".env"
    env.write_text('ES_HOST="localhost"\nES_PASSWORD="x"\nCH_DIRNAME="a"\n')
    settings = Settings(_env_file=str(env))  # type: ignore[call-arg]
    assert not hasattr(settings, "ES_HOST")
