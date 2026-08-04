import sys

from transcript_bot.bot import build_application
from transcript_bot.config import settings
from transcript_bot.database import init_database
from transcript_bot.storage import ensure_data_dirs


def main() -> None:
    try:
        settings.validate_runtime()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc

    ensure_data_dirs(settings.data_dir)
    init_database(settings.data_dir)
    app = build_application(settings.telegram_bot_token)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
