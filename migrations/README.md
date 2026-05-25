# Database migrations

Alembic manages the PostgreSQL schema for Content Fabric.

Useful commands:

```bash
poetry run alembic upgrade head
poetry run alembic downgrade -1
poetry run alembic revision --autogenerate -m "describe change"
```

`DATABASE_URL` is read through `app.db.get_database_url()`, so the same `.env`
contract is used by the bot and migrations.
