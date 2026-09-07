import asyncio
import asyncpg


async def main():
    conn = await asyncpg.connect(
        host="localhost",
        port=5432,
        user="postgres",
        password="postgres",
        database="skincare",
    )
    version = await conn.fetchval("SELECT version()")
    print(f"Connected successfully. Postgres version: {version}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
