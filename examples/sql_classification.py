"""Classify SQL text rows through the same package API, in bounded batches.

Uses SQLite in memory and the synthetic demo selector. This is a SQL-row integration,
not a native MotherDuck/DuckDB UDF or a benchmark of provider quality/performance.
"""
import asyncio
import json
import sqlite3

from unified import create_runtime


async def main():
    runtime, _ = create_runtime()
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)')
        db.executemany('INSERT INTO messages VALUES (?, ?)', [
            (1, 'Please send my invoice'), (2, 'The application will not start'),
        ])
        db.execute('CREATE TABLE classifications (message_id INTEGER, label TEXT, status TEXT, receipt TEXT)')
        cursor = db.execute('SELECT id, text FROM messages ORDER BY id')
        while rows := cursor.fetchmany(100):
            responses = await runtime.decide_many([
                {'policy': 'topics', 'policy_version': '1', 'request_id': f'message-{row_id}',
                 'tenant_id': 'demo', 'input': {'text': text}}
                for row_id, text in rows
            ])
            db.executemany('INSERT INTO classifications VALUES (?, ?, ?, ?)', [
                (row_id, response.result.get('label'), response.status, response.model_dump_json())
                for (row_id, _), response in zip(rows, responses)
            ])
        print(json.dumps(db.execute('SELECT message_id, label, status FROM classifications').fetchall()))


if __name__ == '__main__':
    asyncio.run(main())
