import argparse, json
from .settings import Settings
from .store import Store


def main():
    p = argparse.ArgumentParser(prog="option-desk")
    p.add_argument(
        "command", choices=["serve", "worker", "init", "import", "demo", "preflight"]
    )
    p.add_argument("file", nargs="?")
    args = p.parse_args()
    settings = Settings()
    if args.command == "serve":
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    elif args.command == "worker":
        from .worker import run

        run()
    elif args.command == "preflight":
        from .settings import ROOT

        print(
            json.dumps(
                {
                    "mode": settings.mode,
                    "independent": True,
                    "providers": settings.providers,
                    "schwab_credentials_present": bool(
                        settings.schwab_app_key and settings.schwab_app_secret
                    ),
                    "project_token_present": (
                        ROOT / settings.schwab_token_path
                    ).is_file(),
                    "polygon_key_present": bool(settings.polygon_api_key),
                    "discord_configured": bool(
                        settings.discord_token and settings.discord_channels
                    ),
                    "mongo_configured": bool(settings.mongo_uri),
                    "model_estimation": "disabled",
                },
                indent=2,
            )
        )
    else:
        store = Store(settings.database_url)
        store.migrate()
        if args.command == "import":
            from .domain import SourceEvent

            if not args.file:
                p.error("import requires a JSON file")
            with open(args.file) as f:
                data = json.load(f)
            print(
                json.dumps(
                    {
                        "baseline_ids": [
                            store.ingest(SourceEvent.model_validate(x))
                            for x in (data if isinstance(data, list) else [data])
                        ]
                    }
                )
            )
        elif args.command == "demo":
            if settings.mode != "offline":
                p.error("demo only allowed in offline mode")
            from .demo import seed

            seed(store)
            print("Synthetic fixtures loaded. All records clearly marked DEMO.")


if __name__ == "__main__":
    main()
